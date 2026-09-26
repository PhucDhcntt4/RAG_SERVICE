from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.config import Settings
from app.product_sync.delta_state_repository import ProductDeltaStateRepository
from app.product_sync.change_detector import build_product_change
from app.product_sync.execute_sync import (
    SyncExecutor,
    configure_logging,
    finalize_catalog_payload,
    is_shopify_managed_image,
    write_enabled,
)
from app.product_sync.grouping import group_shopify_products
from app.product_sync.payload_builder import build_catalog_payload
from app.product_sync.shopify_client import (
    ShopifyClient,
    ShopifyConfig,
    aggregate_shopify_status,
    exact_products_for_code,
    product_code_from_shopify,
)

logger = logging.getLogger("rag_service.product_sync.delta")


class DeltaSyncError(RuntimeError):
    pass


def clean(value) -> str:
    return str(value or "").strip()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_iso(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def stable_hash(value) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def source_summary(product: dict) -> dict:
    return {
        "id": clean(product.get("id")),
        "legacy_id": clean(product.get("legacyResourceId")) or None,
        "title": clean(product.get("title")),
        "handle": clean(product.get("handle")),
        "status": clean(product.get("status")).upper() or "UNKNOWN",
        "updated_at": clean(product.get("updatedAt")),
    }


def latest_updated_at(products: list[dict]) -> str:
    values = [
        clean(product.get("updatedAt"))
        for product in products
        if clean(product.get("updatedAt"))
    ]
    return max(values) if values else ""


def apply_status_fields(
    payload: dict,
    *,
    status: str,
    sources: list[dict],
    updated_at: str,
) -> dict:
    result = copy.deepcopy(payload)
    result["status"] = status
    result["shopify_sources"] = sources
    if updated_at:
        result["updated_at"] = updated_at

    public_info = dict(result.get("public_info") or {})
    public_info["status"] = status
    result["public_info"] = public_info

    summary = dict(result.get("summary") or {})
    summary["status"] = status
    if updated_at:
        summary["updated_at"] = updated_at
    if status != "ACTIVE":
        summary["ai_ready"] = False
    result["summary"] = summary

    detail = dict(result.get("detail") or {})
    product = dict(detail.get("product") or {})
    product["status"] = status
    if updated_at:
        product["source_updated_at"] = updated_at
    detail["product"] = product
    result["detail"] = detail
    return result


def product_fingerprint(payload: dict, sources: list[dict], status: str) -> str:
    """Fingerprint product content, intentionally excluding inventory/availability."""
    variants = []
    for row in payload.get("variants") or []:
        variants.append(
            {
                "external_id": clean(row.get("external_id")),
                "sku": clean(row.get("sku")),
                "barcode": clean(row.get("barcode")),
                "variant_title": clean(row.get("variant_title")),
                "color": clean(row.get("color")),
                "size": clean(row.get("size")),
                "price": row.get("price"),
                "compare_at_price": row.get("compare_at_price"),
            }
        )
    variants.sort(
        key=lambda row: (
            row["external_id"],
            row["color"],
            row["size"],
            row["sku"],
        )
    )

    source_state = [
        {
            "id": clean(row.get("id")),
            "status": clean(row.get("status")).upper(),
        }
        for row in sources
    ]
    source_state.sort(key=lambda row: row["id"])

    value = {
        "product_code": clean(payload.get("product_code")),
        "title": clean(payload.get("title")),
        "product_type": clean(payload.get("product_type")),
        "description": clean(payload.get("description")),
        "vendor": clean(payload.get("vendor")),
        "material": clean(payload.get("material")),
        "sole": clean(payload.get("sole")),
        "height": clean(payload.get("height")),
        "status": status,
        "sources": source_state,
        "variants": variants,
    }
    return stable_hash(value)


def image_fingerprint(payload: dict) -> str:
    images = []
    for row in payload.get("images") or []:
        images.append(
            {
                "external_id": clean(row.get("external_id")),
                "color": clean(row.get("color")),
                "source_url": clean(row.get("source_url")),
                "alt_text": clean(row.get("alt_text")),
                "width": row.get("width"),
                "height": row.get("height"),
                "image_order": row.get("image_order"),
            }
        )
    images.sort(
        key=lambda row: (
            row["color"],
            row["image_order"] or 0,
            row["external_id"],
            row["source_url"],
        )
    )
    return stable_hash(images)


def exact_sources_for_code(client: ShopifyClient, product_code: str) -> list[dict]:
    """Load every Shopify product whose first non-empty SKU equals product_code."""
    return exact_products_for_code(client, product_code)


def apply_nonactive_status(
    executor: SyncExecutor,
    *,
    code: str,
    existing_row: dict,
    status: str,
    sources: list[dict],
    current_points: list[dict],
):
    old_payload = existing_row.get("payload") or {}
    payload = copy.deepcopy(old_payload)

    images = []
    for image in payload.get("images") or []:
        item = dict(image)
        item["embedded"] = False
        images.append(item)

    payload = finalize_catalog_payload(payload, images, False)
    source_rows = [source_summary(product) for product in sources]
    updated_at = latest_updated_at(sources)
    payload = apply_status_fields(
        payload,
        status=status,
        sources=source_rows,
        updated_at=updated_at,
    )
    payload["product_fingerprint"] = product_fingerprint(
        payload,
        source_rows,
        status,
    )
    payload["image_fingerprint"] = image_fingerprint(payload)

    # Mark catalog non-retrievable first, then remove only Shopify-managed vectors.
    executor.qdrant.upsert_catalog(existing_row["point_id"], payload)
    managed_points = [
        point for point in current_points if is_shopify_managed_image(point)
    ]
    executor.qdrant.delete_image_points(
        [point["point_id"] for point in managed_points]
    )

    audit_change = build_product_change(
        product_code=code,
        old_payload=old_payload,
        new_payload=payload,
        change_type="INACTIVATED",
    )

    return {
        "product_code": code,
        "action": "STATUS_ONLY",
        "status": status,
        "removed_shopify_vectors": len(managed_points),
        "audit_change": audit_change,
    }


def collect_baseline(
    settings: Settings,
    *,
    progress: Callable[[], None] | None = None,
) -> dict:
    """Read a Delta baseline without publishing its checkpoint yet."""
    cutoff = utc_now()
    client = ShopifyClient(ShopifyConfig.load())
    mapping: dict[str, str] = {}
    loaded = 0

    try:
        for product in client.iter_all_product_keys():
            loaded += 1
            code = product_code_from_shopify(product)
            product_id = clean(product.get("id"))
            if product_id and code:
                mapping[product_id] = code
            if loaded % 500 == 0:
                print(f"Bootstrap loaded: {loaded}")
                if progress is not None:
                    progress()
    finally:
        client.close()

    if progress is not None:
        progress()

    return {
        "cutoff": cutoff,
        "mapping": mapping,
        "loaded_products": loaded,
    }


def activate_baseline(settings: Settings, baseline: dict):
    """Publish a collected baseline after the initial Full Sync succeeds."""
    state_repository = ProductDeltaStateRepository(settings)
    state_repository.initialize()
    state_repository.replace_state(
        last_success_at=baseline["cutoff"],
        product_map=baseline["mapping"],
    )


def bootstrap(settings: Settings):
    """Create the PostgreSQL baseline map and checkpoint from the CLI."""
    baseline = collect_baseline(settings)
    activate_baseline(settings, baseline)

    print()
    print("DELTA SYNC POSTGRESQL BOOTSTRAP OK")
    print(f"Products mapped : {len(baseline['mapping'])}")
    print(f"last_success_at : {iso_z(baseline['cutoff'])}")


def run_delta(
    settings: Settings,
    *,
    apply: bool,
    overlap_seconds: int,
    progress: Callable[[], None] | None = None,
    audit_callback: Callable[[dict], None] | None = None,
) -> dict:
    state_repository = ProductDeltaStateRepository(settings)
    state = state_repository.load()
    previous_success = parse_iso(state["last_success_at"])
    query_start = previous_success - timedelta(seconds=overlap_seconds)
    cutoff = utc_now()
    start_text = iso_z(query_start)
    cutoff_text = iso_z(cutoff)

    old_map = dict(state.get("product_map") or {})
    map_upserts: dict[str, str] = {}
    removed_product_ids: set[str] = set()

    client = ShopifyClient(ShopifyConfig.load())
    executor = SyncExecutor(settings, apply=apply)
    failures = []
    results = []

    try:
        executor.qdrant.validate()
        changed = list(client.iter_changed_products(start_text, cutoff_text))
        if progress is not None:
            progress()
        affected_codes: set[str] = set()

        # A SKU change affects both the previous and current product_code.
        for product in changed:
            product_id = clean(product.get("id"))
            previous_code = old_map.get(product_id)
            current_code = product_code_from_shopify(product)
            if previous_code:
                affected_codes.add(previous_code)
            if current_code:
                affected_codes.add(current_code)

        print()
        print("=" * 72)
        print("PRODUCT DELTA SYNC")
        print("=" * 72)
        print(f"Window       : {start_text} -> {cutoff_text}")
        print(f"Changed      : {len(changed)} Shopify products")
        print(f"Affected SKU : {len(affected_codes)}")
        print(f"Apply        : {apply}")
        print("-" * 72)

        for code in sorted(affected_codes):
            try:
                sources = exact_sources_for_code(client, code)
                source_rows = [source_summary(product) for product in sources]
                status = aggregate_shopify_status(sources)
                updated_at = latest_updated_at(sources)
                existing = executor.qdrant.catalog_by_code(code)
                active_sources = [
                    product
                    for product in sources
                    if clean(product.get("status")).upper() == "ACTIVE"
                ]

                # No ACTIVE Shopify source remains for this SKU.
                if not active_sources:
                    if existing is None:
                        result = {
                            "product_code": code,
                            "action": "SKIP_NEW_NONACTIVE",
                            "status": status,
                        }
                        print(code, result["action"], status)
                        results.append(result)
                        continue

                    old_payload = existing.get("payload") or {}
                    new_fp = product_fingerprint(old_payload, source_rows, status)
                    if (
                        clean(old_payload.get("product_fingerprint")) == new_fp
                        and clean(old_payload.get("status")).upper() == status
                    ):
                        result = {
                            "product_code": code,
                            "action": "SKIP_UNCHANGED",
                            "status": status,
                        }
                        print(code, result["action"], status)
                        results.append(result)
                        continue

                    if not apply:
                        result = {
                            "product_code": code,
                            "action": "STATUS_ONLY",
                            "status": status,
                        }
                        print(code, "DRY", result["action"], status)
                        results.append(result)
                        continue

                    current_points = executor.qdrant.images_by_code(code)
                    result = apply_nonactive_status(
                        executor,
                        code=code,
                        existing_row=existing,
                        status=status,
                        sources=sources,
                        current_points=current_points,
                    )
                    print(code, result["action"], status)
                    results.append(result)
                    continue

                # At least one source is ACTIVE: build the Product RAG from ACTIVE sources.
                groups, _ = group_shopify_products(active_sources)
                group = groups.get(code)
                if group is None:
                    raise RuntimeError(f"Không build được ProductGroup cho {code}")

                old_payload = existing.get("payload") if existing else None
                preview_payload, _ = build_catalog_payload(group, old_payload)
                preview_payload = apply_status_fields(
                    preview_payload,
                    status=status,
                    sources=source_rows,
                    updated_at=updated_at,
                )

                new_product_fp = product_fingerprint(
                    preview_payload,
                    source_rows,
                    status,
                )
                new_image_fp = image_fingerprint(preview_payload)
                old_product_fp = clean((old_payload or {}).get("product_fingerprint"))
                old_image_fp = clean((old_payload or {}).get("image_fingerprint"))

                if (
                    existing is not None
                    and old_product_fp == new_product_fp
                    and old_image_fp == new_image_fp
                ):
                    result = {
                        "product_code": code,
                        "action": "SKIP_UNCHANGED",
                        "status": status,
                    }
                    print(code, result["action"])
                    results.append(result)
                    continue

                images = preview_payload.get("images") or []
                image_state_complete = all(
                    bool(image.get("embedded")) for image in images
                )
                image_structure_same = (
                    existing is not None
                    and bool(old_image_fp)
                    and old_image_fp == new_image_fp
                )

                # Product metadata changed, but image set did not.
                if image_structure_same and image_state_complete:
                    if not apply:
                        result = {
                            "product_code": code,
                            "action": "CATALOG_ONLY",
                            "status": status,
                        }
                        print(code, "DRY", result["action"])
                        results.append(result)
                        continue

                    ai_ready = status == "ACTIVE" and bool(images) and image_state_complete
                    final_payload = finalize_catalog_payload(
                        preview_payload,
                        images,
                        ai_ready,
                    )
                    final_payload = apply_status_fields(
                        final_payload,
                        status=status,
                        sources=source_rows,
                        updated_at=updated_at,
                    )
                    final_payload["product_fingerprint"] = new_product_fp
                    final_payload["image_fingerprint"] = new_image_fp
                    executor.qdrant.upsert_catalog(
                        existing["point_id"],
                        final_payload,
                    )
                    audit_change = build_product_change(
                        product_code=code,
                        old_payload=old_payload,
                        new_payload=final_payload,
                    )
                    result = {
                        "product_code": code,
                        "action": "CATALOG_ONLY",
                        "status": status,
                        "new_embeddings": 0,
                        "audit_change": audit_change,
                    }
                    print(code, result["action"])
                    results.append(result)
                    continue

                # Product is new, or image structure changed, or legacy payload lacks fingerprints.
                if not apply:
                    result = {
                        "product_code": code,
                        "action": "CREATE_FULL" if existing is None else "FULL_RECONCILE",
                        "status": status,
                    }
                    print(code, "DRY", result["action"])
                    results.append(result)
                    continue

                current_points = executor.qdrant.images_by_code(code)

                def enrich(final_payload: dict) -> dict:
                    final_payload = apply_status_fields(
                        final_payload,
                        status=status,
                        sources=source_rows,
                        updated_at=updated_at,
                    )
                    final_payload["product_fingerprint"] = new_product_fp
                    final_payload["image_fingerprint"] = new_image_fp
                    return final_payload

                result = executor.apply_active_product(
                    code,
                    group,
                    existing,
                    current_points,
                    payload_enricher=enrich,
                )
                result["action"] = (
                    "CREATE_FULL" if existing is None else "FULL_RECONCILE"
                )
                print(
                    code,
                    result["action"],
                    f"embeddings={result.get('new_embeddings', 0)}",
                )
                results.append(result)

            except Exception as exc:
                logger.exception("Delta sync failed product_code=%s", code)
                failures.append(
                    {
                        "product_code": code,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            finally:
                if progress is not None:
                    progress()

        # Prepare mapping changes only after every affected SKU was processed.
        for product in changed:
            product_id = clean(product.get("id"))
            current_code = product_code_from_shopify(product)
            if not product_id:
                continue
            if current_code:
                map_upserts[product_id] = current_code
                removed_product_ids.discard(product_id)
            else:
                removed_product_ids.add(product_id)
                map_upserts.pop(product_id, None)

        if failures:
            print()
            print("DELTA FAILED - KHÔNG cập nhật last_success_at")
            for failure in failures:
                print(
                    f"- {failure['product_code']}: "
                    f"{failure['error_type']} - {failure['error']}"
                )
            raise DeltaSyncError(
                f"Delta Sync failed for {len(failures)} product_code; "
                "checkpoint was not updated"
            )

        audit_changes = [
            row["audit_change"]
            for row in results
            if row.get("audit_change") is not None
        ]

        # Persist audit before advancing the checkpoint. If audit persistence
        # fails, the checkpoint remains unchanged and the run is safely retried.
        if apply and audit_callback is not None:
            for change in audit_changes:
                audit_callback(change)

        # Dry-run intentionally does not move the checkpoint.
        if apply:
            state_repository.commit_success(
                cutoff=cutoff,
                upserts=map_upserts,
                removed_ids=removed_product_ids,
            )

        print("-" * 72)
        print(f"Processed affected SKU: {len(results)}")
        print(f"PostgreSQL checkpoint : {'updated' if apply else 'unchanged'}")
        print("=" * 72)

        actions = [clean(row.get("action")) for row in results]
        return {
            "changed_products": len(changed),
            "affected_products": len(affected_codes),
            "processed_products": len(results),
            "skipped_products": sum(
                action.startswith("SKIP_") for action in actions
            ),
            "created_products": actions.count("CREATE_FULL"),
            "updated_products": sum(
                action in {"CATALOG_ONLY", "FULL_RECONCILE"}
                for action in actions
            ),
            "inactivated_products": actions.count("STATUS_ONLY"),
            "new_embeddings": sum(
                int(row.get("new_embeddings") or 0) for row in results
            ),
            "removed_image_points": sum(
                int(row.get("removed_image_points") or 0)
                + int(row.get("removed_shopify_vectors") or 0)
                for row in results
            ),
            "checkpoint_updated": apply,
            "audit_changes": audit_changes,
        }

    finally:
        executor.close()
        client.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Incremental Product RAG Delta Sync"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Ghi thay đổi vào Qdrant",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Khởi tạo checkpoint và Shopify Product ID -> SKU mapping",
    )
    parser.add_argument(
        "--overlap-seconds",
        type=int,
        default=120,
        help="Đọc lùi checkpoint để tránh mất thay đổi ở biên thời gian",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    settings = Settings.load()
    configure_logging(settings.log_level)

    if args.apply and not write_enabled():
        raise SystemExit("PRODUCT_SYNC_WRITE_ENABLED chưa bật")

    if args.bootstrap:
        bootstrap(settings)
        return

    try:
        run_delta(
            settings,
            apply=args.apply,
            overlap_seconds=max(0, args.overlap_seconds),
        )
    except DeltaSyncError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
