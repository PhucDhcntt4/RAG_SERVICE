from __future__ import annotations

import argparse
import copy
import json
import logging
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from app.config import ROOT, Settings
from app.product_sync.payload_builder import normalize_vi
from app.product_sync.qdrant_store import ProductQdrantStore
from app.product_sync.shopify_client import (
    ShopifyClient,
    ShopifyConfig,
    product_code_from_shopify,
)

logger = logging.getLogger("rag_service.product_sync.inventory")


def clean(value) -> str:
    return str(value or "").strip()


def selected_option(variant: dict, *names: str) -> str:
    wanted = {normalize_vi(name) for name in names}
    for row in variant.get("selectedOptions") or []:
        if normalize_vi(row.get("name")) in wanted:
            return clean(row.get("value"))
    return ""


def remote_variant(raw: dict) -> dict:
    return {
        "external_id": clean(raw.get("id")),
        "legacy_id": clean(raw.get("legacyResourceId")),
        "sku": clean(raw.get("sku")),
        "color": selected_option(raw, "Color", "Colour", "Màu", "Màu sắc"),
        "size": selected_option(raw, "Size", "Kích cỡ", "Cỡ"),
        "inventory_quantity": raw.get("inventoryQuantity"),
        "available": bool(raw.get("availableForSale")),
    }


def unique(values):
    result = []
    seen = set()
    for value in values:
        marker = str(value)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def build_inventory_snapshot(progress: Callable[[], None] | None = None):
    client = ShopifyClient(ShopifyConfig.load())
    by_code: dict[str, list[dict]] = defaultdict(list)
    product_count = 0
    variant_count = 0
    skipped_without_code = 0
    try:
        for product in client.iter_active_inventory_products():
            product_count += 1
            code = product_code_from_shopify(product)
            if not code:
                skipped_without_code += 1
                continue
            rows = ((product.get("variants") or {}).get("nodes") or [])
            for raw in rows:
                row = remote_variant(raw)
                if not row["external_id"] and not row["legacy_id"] and not row["sku"]:
                    continue
                by_code[code].append(row)
                variant_count += 1
            if progress and product_count % 50 == 0:
                progress()
            if product_count % 100 == 0:
                logger.info("Inventory Shopify ACTIVE loaded=%s variants=%s", product_count, variant_count)
    finally:
        client.close()

    return {
        "products": product_count,
        "variants": variant_count,
        "skipped_without_code": skipped_without_code,
        "by_code": dict(by_code),
    }


def _variant_indexes(rows: list[dict]):
    by_external = {}
    by_legacy = {}
    by_composite: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("external_id"):
            by_external[row["external_id"]] = row
        if row.get("legacy_id"):
            by_legacy[row["legacy_id"]] = row
        key = (
            clean(row.get("sku")),
            normalize_vi(row.get("color")),
            normalize_vi(row.get("size")),
        )
        by_composite[key].append(row)
    return by_external, by_legacy, dict(by_composite)


def _find_remote_variant(local: dict, indexes):
    by_external, by_legacy, by_composite = indexes
    external_id = clean(local.get("external_id"))
    if external_id and external_id in by_external:
        return by_external[external_id]
    legacy_id = clean(local.get("legacy_id"))
    if legacy_id and legacy_id in by_legacy:
        return by_legacy[legacy_id]
    key = (
        clean(local.get("sku")),
        normalize_vi(local.get("color")),
        normalize_vi(local.get("size")),
    )
    matches = by_composite.get(key) or []
    return matches[0] if len(matches) == 1 else None


def _refresh_public_inventory(payload: dict, variants: list[dict], synced_at: str, complete: bool):
    public = dict(payload.get("public_info") or {})

    availability_by_color = {}
    colors = unique([clean(row.get("color")) for row in variants if clean(row.get("color"))])
    for color in colors:
        matching = [row for row in variants if clean(row.get("color")) == color]
        availability_by_color[color] = {
            "available": any(bool(row.get("available")) for row in matching),
            "available_sizes": unique([
                clean(row.get("size"))
                for row in matching
                if clean(row.get("size")) and bool(row.get("available"))
            ]),
        }
    public["availability_by_color"] = availability_by_color

    availability_lookup = {
        (clean(row.get("color")), clean(row.get("size"))): bool(row.get("available"))
        for row in variants
    }
    variant_prices = []
    for price_row in public.get("variant_prices") or []:
        item = dict(price_row)
        key = (clean(item.get("color")), clean(item.get("size")))
        if key in availability_lookup:
            item["available"] = availability_lookup[key]
        variant_prices.append(item)
    public["variant_prices"] = variant_prices

    public["inventory_synced_at"] = synced_at
    public["inventory_sync_complete"] = bool(complete)
    payload["public_info"] = public


def update_catalog_inventory(payload: dict, remote_rows: list[dict], synced_at: str):
    result = copy.deepcopy(payload)
    local_variants = [dict(row) for row in (result.get("variants") or [])]
    indexes = _variant_indexes(remote_rows)
    changed_variants = 0
    missing_variants = 0

    for row in local_variants:
        remote = _find_remote_variant(row, indexes)
        if remote is None:
            missing_variants += 1
            continue
        old_qty = row.get("inventory_quantity")
        old_available = bool(row.get("available"))
        new_qty = remote.get("inventory_quantity")
        new_available = bool(remote.get("available"))
        if old_qty != new_qty or old_available != new_available:
            changed_variants += 1
        row["inventory_quantity"] = new_qty
        row["available"] = new_available

    complete = missing_variants == 0
    result["variants"] = local_variants

    detail = dict(result.get("detail") or {})
    detail["variants"] = copy.deepcopy(local_variants)
    result["detail"] = detail

    _refresh_public_inventory(result, local_variants, synced_at, complete)

    result["inventory_source"] = "shopify"
    result["inventory_synced_at"] = synced_at
    result["inventory_sync_complete"] = complete
    result["inventory_missing_variants"] = missing_variants

    summary = dict(result.get("summary") or {})
    summary["inventory_synced_at"] = synced_at
    summary["inventory_sync_complete"] = complete
    summary["inventory_missing_variants"] = missing_variants
    summary["available_variant_count"] = sum(1 for row in local_variants if bool(row.get("available")))
    result["summary"] = summary

    return result, changed_variants, missing_variants


class InventorySyncExecutor:
    def __init__(self, settings, *, apply: bool):
        self.settings = settings
        self.apply = apply
        self.qdrant = ProductQdrantStore(settings)

    def close(self):
        self.qdrant.close()

    def run(self, progress: Callable[[], None] | None = None, max_products: int | None = None):
        self.qdrant.validate()
        existing = self.qdrant.all_catalog()
        snapshot = build_inventory_snapshot(progress)
        remote_by_code = snapshot["by_code"]
        synced_at = datetime.now(UTC).isoformat(timespec="seconds")

        stats = {
            "shopify_products": snapshot["products"],
            "shopify_variants": snapshot["variants"],
            "skipped_without_code": snapshot["skipped_without_code"],
            "checked_products": 0,
            "updated_products": 0,
            "checked_variants": 0,
            "changed_variants": 0,
            "missing_products": 0,
            "missing_variants": 0,
        }

        pending = []
        rows = [
            (code, entry)
            for code, entry in sorted(existing.items())
            if str((entry.get("payload") or {}).get("status") or "").upper() == "ACTIVE"
        ]
        if max_products is not None:
            rows = rows[: max(0, int(max_products))]

        for index, (code, entry) in enumerate(rows, start=1):
            remote_rows = remote_by_code.get(code)
            if not remote_rows:
                stats["missing_products"] += 1
                continue

            payload = entry.get("payload") or {}
            local_variants = payload.get("variants") or []
            stats["checked_products"] += 1
            stats["checked_variants"] += len(local_variants)

            updated_payload, changed_variants, missing_variants = update_catalog_inventory(
                payload,
                remote_rows,
                synced_at,
            )
            stats["changed_variants"] += changed_variants
            stats["missing_variants"] += missing_variants
            if changed_variants:
                stats["updated_products"] += 1

            # Every checked catalog point gets a fresh inventory_synced_at timestamp.
            # This is still lightweight: catalog-only batch upsert, no image/CLIP work.
            pending.append({"point_id": entry["point_id"], "payload": updated_payload})

            if progress and index % 50 == 0:
                progress()

        if self.apply and pending:
            self.qdrant.upsert_catalog_batch(pending, chunk_size=100)

        return stats


def configure_logging(level):
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def write_report(stats: dict, *, apply: bool):
    output_dir = ROOT / "tmp" / "product_inventory_sync"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"inventory_{stamp}.json"
    path.write_text(
        json.dumps({"generated_at": datetime.now().isoformat(), "apply": apply, "stats": stats}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def parse_args():
    parser = argparse.ArgumentParser(description="Inventory-only Shopify -> Product RAG sync")
    parser.add_argument("--apply", action="store_true", help="Ghi catalog Qdrant. Không đụng image vectors.")
    parser.add_argument("--max-products", type=int, default=None)
    return parser.parse_args()


def main():
    args = parse_args()
    settings = Settings.load()
    configure_logging(settings.log_level)
    if args.apply:
        from app.product_sync.execute_sync import write_enabled
        if not write_enabled():
            raise SystemExit("TỪ CHỐI GHI: PRODUCT_SYNC_WRITE_ENABLED=false")

    executor = InventorySyncExecutor(settings, apply=args.apply)
    try:
        stats = executor.run(max_products=args.max_products)
    finally:
        executor.close()

    report = write_report(stats, apply=args.apply)
    print("=" * 78)
    print("INVENTORY-ONLY SYNC")
    print("=" * 78)
    for key, value in stats.items():
        print(f"{key:28}: {value}")
    print(f"Report                      : {report}")
    print("Qdrant image vectors        : KHÔNG ĐỤNG TỚI")
    print("OpenCLIP                    : KHÔNG LOAD")
    print("=" * 78)


if __name__ == "__main__":
    main()
