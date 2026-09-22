from __future__ import annotations

import json
import logging
from datetime import datetime

from app.config import ROOT, Settings
from app.product_repository import ProductRepository
from app.product_sync.grouping import group_shopify_products
from app.product_sync.shopify_client import ShopifyClient, ShopifyConfig

logger = logging.getLogger("rag_service.product_sync.grouped_dry_run")


def configure_logging(level):
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_existing_codes(settings) -> set[str]:
    repo = ProductRepository(settings)
    try:
        result: set[str] = set()
        cursor = None

        while True:
            page = repo.list_products(limit=100, cursor=cursor)

            for product in page.get("products") or []:
                code = str(product.get("product_code") or "").strip()
                if code:
                    result.add(code)

            cursor = page.get("next_cursor")
            if not cursor:
                break

        return result
    finally:
        repo.close()


def main():
    settings = Settings.load()
    configure_logging(settings.log_level)

    client = ShopifyClient(ShopifyConfig.load())

    try:
        products: list[dict] = []

        logger.info("Đang tải Shopify ACTIVE products...")

        for index, product in enumerate(client.iter_active_products(), start=1):
            products.append(product)
            if index % 100 == 0:
                logger.info("Shopify ACTIVE loaded=%s", index)

    finally:
        client.close()

    logger.info("Đang group Shopify products theo product_code/SKU...")
    groups, skipped = group_shopify_products(products)

    existing_codes = load_existing_codes(settings)
    group_codes = set(groups)

    existing_active_codes = existing_codes & group_codes
    existing_inactive_codes = existing_codes - group_codes
    new_active_codes = group_codes - existing_codes

    repeated_color_groups = [
        group for group in groups.values()
        if group.source_product_count > 1
    ]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = ROOT / "tmp" / "product_sync_grouped" / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)

    summaries = [
        groups[code].summary()
        for code in sorted(groups)
    ]

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "shopify_active_records": len(products),
        "product_groups": len(groups),
        "skipped_without_product_code": len(skipped),
        "groups_with_multiple_shopify_products": len(repeated_color_groups),
        "qdrant_existing_codes": len(existing_codes),
        "existing_active_codes": len(existing_active_codes),
        "existing_not_active_codes": len(existing_inactive_codes),
        "new_active_codes": len(new_active_codes),
        "existing_not_active_product_codes": sorted(existing_inactive_codes),
        "new_active_product_codes": sorted(new_active_codes),
        "groups": summaries,
    }

    (output_dir / "grouped_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Always write JC53 preview if present because it is the known example.
    jc53 = groups.get("JC53")
    if jc53:
        (output_dir / "JC53_preview.json").write_text(
            json.dumps(
                jc53.preview_payload(),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # Also write a few multi-source groups for quick review.
    sample_groups = sorted(
        repeated_color_groups,
        key=lambda item: (-item.source_product_count, item.product_code),
    )[:10]

    (output_dir / "multi_color_samples.json").write_text(
        json.dumps(
            [group.preview_payload() for group in sample_groups],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print("=" * 72)
    print("PRODUCT SYNC - PHASE 2A GROUPED DRY-RUN")
    print("=" * 72)
    print(f"Shopify ACTIVE records             : {len(products)}")
    print(f"Grouped product_code/SKU           : {len(groups)}")
    print(f"Skipped without product_code       : {len(skipped)}")
    print(f"Groups with multiple Shopify items : {len(repeated_color_groups)}")
    print("-" * 72)
    print(f"Qdrant existing product_code       : {len(existing_codes)}")
    print(f"Existing + ACTIVE                  : {len(existing_active_codes)}")
    print(f"Existing but no longer ACTIVE      : {len(existing_inactive_codes)}")
    print(f"New ACTIVE product_code            : {len(new_active_codes)}")
    print("-" * 72)

    if jc53:
        summary = jc53.summary()
        print("JC53")
        print(f"  Shopify source products : {summary['source_product_count']}")
        print(f"  Colors                  : {', '.join(summary['colors']) or '(không đọc được màu từ option)'}")
        print(f"  Variants                : {summary['variant_count']}")
        print(f"  Images                  : {summary['image_count']}")
        print(f"  Latest updated_at       : {summary['latest_updated_at']}")
    else:
        print("JC53: không tìm thấy trong Shopify ACTIVE.")

    print("-" * 72)
    print(f"Report folder: {output_dir}")
    print("Files:")
    print("  grouped_summary.json")
    print("  JC53_preview.json              (nếu JC53 tồn tại)")
    print("  multi_color_samples.json")
    print("=" * 72)
    print("DRY-RUN ONLY: chưa ghi Qdrant, chưa tạo CLIP embedding.")
    print()


if __name__ == "__main__":
    main()
