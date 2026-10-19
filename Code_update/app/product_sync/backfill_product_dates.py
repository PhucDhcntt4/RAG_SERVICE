from __future__ import annotations

import argparse
import logging
import os

from dotenv import dotenv_values

from app.config import ROOT, Settings
from app.product_sync.grouping import group_shopify_products
from app.product_sync.qdrant_store import ProductQdrantStore
from app.product_sync.shopify_client import ShopifyClient, ShopifyConfig


logger = logging.getLogger("rag_service.product_sync.backfill_dates")


def clean(value) -> str:
    return str(value or "").strip()


def write_enabled() -> bool:
    """Read the existing write guard without importing the CLIP sync stack."""
    values = {**dotenv_values(ROOT / ".env"), **os.environ}
    return clean(values.get("PRODUCT_SYNC_WRITE_ENABLED")).casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Backfill created_at/published_at/new_at cho catalog Qdrant theo "
            "product_code. Mặc định chỉ xem trước; --apply mới ghi dữ liệu."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Thực sự cập nhật payload Qdrant.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Số Shopify product mỗi trang (1-100).",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=20,
        help="Số thay đổi mẫu in ra màn hình.",
    )
    return parser.parse_args()


def run_backfill(*, apply: bool, page_size: int = 100, show: int = 20) -> dict:
    if apply and not write_enabled():
        raise RuntimeError(
            "TỪ CHỐI GHI: hãy đặt PRODUCT_SYNC_WRITE_ENABLED=true trước khi dùng --apply."
        )

    shopify = ShopifyClient(ShopifyConfig.load())
    qdrant = ProductQdrantStore(Settings.load())

    try:
        shopify_rows = list(
            shopify.iter_all_product_dates(
                page_size=min(max(int(page_size), 1), 100)
            )
        )
        groups, skipped_without_code = group_shopify_products(shopify_rows)
        catalog = qdrant.all_catalog()
        catalog_by_folded_code = {
            clean(code).casefold(): row
            for code, row in catalog.items()
            if clean(code)
        }

        planned = []
        unchanged = 0
        missing_qdrant = 0
        missing_new_at = 0

        for product_code, group in groups.items():
            new_at = group.new_at
            if not new_at:
                missing_new_at += 1
                continue

            point = catalog_by_folded_code.get(clean(product_code).casefold())
            if point is None:
                missing_qdrant += 1
                continue

            dates = {
                "created_at": group.created_at,
                "published_at": group.published_at,
                "new_at": new_at,
            }
            current = point.get("payload") or {}
            if all(clean(current.get(key)) == value for key, value in dates.items()):
                unchanged += 1
                continue

            planned.append(
                {
                    "point_id": point.get("point_id"),
                    "product_code": product_code,
                    "before": {
                        key: clean(current.get(key))
                        for key in dates
                    },
                    "after": dates,
                }
            )

        preview_count = max(0, int(show))
        for row in planned[:preview_count]:
            print(
                f"{row['product_code']}: "
                f"new_at={row['before']['new_at'] or '-'} -> "
                f"{row['after']['new_at']}"
            )

        if apply:
            for row in planned:
                qdrant.set_catalog_payload(row["point_id"], row["after"])

        result = {
            "mode": "APPLY" if apply else "DRY_RUN",
            "shopify_products": len(shopify_rows),
            "grouped_product_codes": len(groups),
            "shopify_without_product_code": len(skipped_without_code),
            "qdrant_catalog_products": len(catalog),
            "planned_updates": len(planned),
            "written_updates": len(planned) if apply else 0,
            "unchanged": unchanged,
            "missing_in_qdrant": missing_qdrant,
            "missing_new_at": missing_new_at,
        }

        print("\nPRODUCT DATE BACKFILL")
        for key, value in result.items():
            print(f"{key}: {value}")
        return result
    finally:
        shopify.close()
        qdrant.close()


def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_backfill(
        apply=args.apply,
        page_size=args.page_size,
        show=args.show,
    )


if __name__ == "__main__":
    main()
