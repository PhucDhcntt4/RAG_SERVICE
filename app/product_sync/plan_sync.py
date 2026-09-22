from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

import httpx

from app.config import ROOT, Settings
from app.product_sync.grouping import group_shopify_products
from app.product_sync.payload_builder import build_catalog_payload, inactive_payload
from app.product_sync.shopify_client import ShopifyClient, ShopifyConfig

logger = logging.getLogger("rag_service.product_sync.plan_sync")


def configure_logging(level):
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def catalog_semantic_view(payload: dict) -> dict:
    """Fields that affect catalog text/search meaning.

    Runtime-only image bookkeeping such as local_path/checksum/embedded is
    excluded so unchanged product text does not force a catalog re-embed.
    """
    return {
        "product_code": payload.get("product_code"),
        "title": payload.get("title"),
        "product_type": payload.get("product_type"),
        "description": payload.get("description"),
        "vendor": payload.get("vendor"),
        "material": payload.get("material"),
        "sole": payload.get("sole"),
        "height": payload.get("height"),
        "status": payload.get("status"),
        "variant_skus": payload.get("variant_skus"),
        "public_info": {
            key: (payload.get("public_info") or {}).get(key)
            for key in (
                "product_name",
                "product_type",
                "description",
                "material",
                "sole",
                "height",
                "status",
                "prices",
                "variant_prices",
                "colors",
                "available_sizes",
                "availability_by_color",
            )
        },
    }


class QdrantCatalogReader:
    def __init__(self, settings):
        self.base_url = settings.qdrant_url.rstrip("/")
        self.collection = settings.product_catalog_collection
        self.timeout = settings.qdrant_timeout_seconds
        self.client = httpx.Client(timeout=self.timeout)

    def close(self):
        self.client.close()

    def all_payloads(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        offset = None

        while True:
            body = {
                "limit": 100,
                "with_payload": True,
                "with_vector": False,
            }
            if offset is not None:
                body["offset"] = offset

            response = self.client.post(
                f"{self.base_url}/collections/{self.collection}/points/scroll",
                json=body,
            )
            response.raise_for_status()
            data = response.json().get("result") or {}

            for point in data.get("points") or []:
                payload = point.get("payload") or {}
                code = str(payload.get("product_code") or "").strip()
                if code:
                    result[code] = {
                        "point_id": point.get("id"),
                        "payload": payload,
                    }

            offset = data.get("next_page_offset")
            if offset is None:
                break

        return result


def load_shopify_groups():
    client = ShopifyClient(ShopifyConfig.load())
    rows = []
    try:
        for index, product in enumerate(client.iter_active_products(), start=1):
            rows.append(product)
            if index % 100 == 0:
                logger.info("Shopify ACTIVE loaded=%s", index)
    finally:
        client.close()

    groups, skipped = group_shopify_products(rows)
    return rows, groups, skipped


def main():
    settings = Settings.load()
    configure_logging(settings.log_level)

    logger.info("Đang đọc Qdrant catalog payload...")
    qdrant = QdrantCatalogReader(settings)
    try:
        existing = qdrant.all_payloads()
    finally:
        qdrant.close()

    logger.info("Đang tải Shopify ACTIVE...")
    shopify_rows, groups, skipped_without_code = load_shopify_groups()

    existing_codes = set(existing)
    active_codes = set(groups)

    create_codes = sorted(active_codes - existing_codes)
    existing_active_codes = sorted(active_codes & existing_codes)
    inactivate_codes = sorted(existing_codes - active_codes)

    create_items = []
    update_items = []
    unchanged_items = []
    inactivate_items = []

    catalog_reembed_count = 0
    image_embed_add_count = 0
    image_remove_count = 0

    preview_payloads = {}

    for code in sorted(active_codes):
        existing_payload = (existing.get(code) or {}).get("payload")
        payload, diff = build_catalog_payload(groups[code], existing_payload)

        if existing_payload is None:
            action = "CREATE"
            catalog_changed = True
        else:
            catalog_changed = (
                fingerprint(catalog_semantic_view(payload))
                != fingerprint(catalog_semantic_view(existing_payload))
            )
            image_changed = bool(diff["added_count"] or diff["removed_count"])
            payload_changed = fingerprint(payload) != fingerprint(existing_payload)

            if not payload_changed and not catalog_changed and not image_changed:
                action = "UNCHANGED"
            else:
                action = "UPDATE"

        diff["requires_catalog_embedding"] = bool(catalog_changed)
        if diff["requires_catalog_embedding"]:
            catalog_reembed_count += 1

        image_embed_add_count += diff["added_count"]
        image_remove_count += diff["removed_count"]

        item = {
            "product_code": code,
            "action": action,
            "source_product_count": groups[code].source_product_count,
            "colors": payload["public_info"]["colors"],
            "variant_count": payload["summary"]["variant_count"],
            "image_count": payload["summary"]["image_count"],
            "catalog_reembed": diff["requires_catalog_embedding"],
            "image_add": diff["added_count"],
            "image_remove": diff["removed_count"],
            "image_unchanged": diff["unchanged_count"],
            "latest_updated_at": payload["updated_at"],
        }

        if action == "CREATE":
            create_items.append(item)
        elif action == "UPDATE":
            update_items.append(item)
        else:
            unchanged_items.append(item)

        if code == "JC53" or (
            len(preview_payloads) < 3 and action in {"CREATE", "UPDATE"}
        ):
            preview_payloads[code] = {
                "payload": payload,
                "diff": diff,
            }

    for code in inactivate_codes:
        old_payload = existing[code]["payload"]
        old_status = str(old_payload.get("status") or "").upper()
        old_ai_ready = bool((old_payload.get("summary") or {}).get("ai_ready"))

        already_inactive = old_status == "INACTIVE" and not old_ai_ready
        action = "UNCHANGED_INACTIVE" if already_inactive else "INACTIVATE"

        if not already_inactive:
            inactivate_items.append(
                {
                    "product_code": code,
                    "action": action,
                    "old_status": old_status,
                    "old_ai_ready": old_ai_ready,
                    "preview_payload": inactive_payload(old_payload),
                }
            )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = ROOT / "tmp" / "product_sync_plan" / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "all_active_plan",
        "write_enabled": False,
        "counts": {
            "shopify_active_records": len(shopify_rows),
            "grouped_product_codes": len(groups),
            "skipped_without_product_code": len(skipped_without_code),
            "qdrant_existing_codes": len(existing_codes),
            "create": len(create_items),
            "update": len(update_items),
            "unchanged": len(unchanged_items),
            "inactivate": len(inactivate_items),
            "catalog_semantic_changes": catalog_reembed_count,
            "catalog_reembed": catalog_reembed_count,
            "image_embeddings_to_add": image_embed_add_count,
            "image_vectors_to_remove": image_remove_count,
        },
        "create": create_items,
        "update": update_items,
        "unchanged": unchanged_items,
        "inactivate": inactivate_items,
    }

    (output_dir / "sync_plan.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "payload_previews.json").write_text(
        json.dumps(preview_payloads, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if "JC53" in preview_payloads:
        (output_dir / "JC53_payload_preview.json").write_text(
            json.dumps(
                preview_payloads["JC53"],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    counts = report["counts"]

    print()
    print("=" * 76)
    print("PRODUCT SYNC - PHASE 2B PAYLOAD + DIFF PLAN")
    print("=" * 76)
    print(f"Shopify ACTIVE records          : {counts['shopify_active_records']}")
    print(f"Grouped product_code            : {counts['grouped_product_codes']}")
    print(f"Skipped without product_code    : {counts['skipped_without_product_code']}")
    print(f"Qdrant existing product_code    : {counts['qdrant_existing_codes']}")
    print("-" * 76)
    print(f"CREATE new catalog points       : {counts['create']}")
    print(f"UPDATE existing catalog points  : {counts['update']}")
    print(f"UNCHANGED catalog points        : {counts['unchanged']}")
    print(f"INACTIVATE old product codes    : {counts['inactivate']}")
    print("-" * 76)
    print(f"Catalog semantic changes        : {counts['catalog_reembed']}")
    print(f"New image embeddings required   : {counts['image_embeddings_to_add']}")
    print(f"Old image vectors to remove     : {counts['image_vectors_to_remove']}")
    print("-" * 76)

    if "JC53" in preview_payloads:
        jc53 = preview_payloads["JC53"]
        payload = jc53["payload"]
        diff = jc53["diff"]
        print("JC53 PREVIEW")
        print(f"  title             : {payload['title']}")
        print(f"  colors            : {', '.join(payload['public_info']['colors'])}")
        print(f"  variants          : {payload['summary']['variant_count']}")
        print(f"  images            : {payload['summary']['image_count']}")
        print(f"  image add/remove  : {diff['added_count']} / {diff['removed_count']}")
        print(f"  image unchanged   : {diff['unchanged_count']}")
        print(f"  ai_ready preview  : {payload['summary']['ai_ready']}")
    else:
        print("JC53 PREVIEW: không nằm trong nhóm cần preview.")

    print("-" * 76)
    print(f"Report folder: {output_dir}")
    print("  sync_plan.json")
    print("  payload_previews.json")
    print("  JC53_payload_preview.json (nếu có)")
    print("=" * 76)
    print("WRITE ENABLED: FALSE")
    print("Phase 2B chỉ lập kế hoạch; chưa ghi/xóa Qdrant.")
    print()


if __name__ == "__main__":
    main()
