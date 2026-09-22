from __future__ import annotations

import csv
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from app.config import ROOT, Settings
from app.product_repository import ProductRepository
from app.product_sync.shopify_client import (
    ShopifyClient,
    ShopifyConfig,
    product_code_from_shopify,
    variant_skus_from_shopify,
)

logger = logging.getLogger("rag_service.product_sync.audit")


def clean_code(value) -> str:
    return str(value or "").strip()


def normalized_code(value) -> str:
    return clean_code(value).casefold()


def configure_logging(level):
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def scan_qdrant(settings) -> tuple[dict, list[dict]]:
    repo = ProductRepository(settings)
    try:
        status = repo.collection_status()
        catalog = status.get("catalog") or {}
        points_count = int(catalog.get("points_count") or 0)

        rows: list[dict] = []
        cursor = None

        while True:
            page = repo.list_products(limit=100, cursor=cursor)
            products = page.get("products") or []

            for item in products:
                rows.append(
                    {
                        "point_id": item.get("point_id"),
                        "product_code": clean_code(item.get("product_code")),
                        "title": item.get("title") or "",
                        "status": item.get("status") or "",
                        "updated_at": item.get("updated_at") or "",
                    }
                )

            cursor = page.get("next_cursor")
            if not cursor:
                break

        return {
            "collection": catalog.get("name") or "",
            "points_count": points_count,
            "rows_scanned": len(rows),
        }, rows
    finally:
        repo.close()


def scan_shopify() -> list[dict]:
    client = ShopifyClient(ShopifyConfig.load())
    rows: list[dict] = []

    try:
        for index, product in enumerate(client.iter_active_products(), start=1):
            skus = variant_skus_from_shopify(product)
            code = product_code_from_shopify(product)

            rows.append(
                {
                    "shopify_id": product.get("id") or "",
                    "legacy_resource_id": product.get("legacyResourceId") or "",
                    "title": product.get("title") or "",
                    "handle": product.get("handle") or "",
                    "updated_at": product.get("updatedAt") or "",
                    "current_rule_code": code or "",
                    "variant_skus": skus,
                }
            )

            if index % 100 == 0:
                logger.info("Shopify ACTIVE scanned=%s", index)

        return rows
    finally:
        client.close()


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]):
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    settings = Settings.load()
    configure_logging(settings.log_level)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = ROOT / "tmp" / "product_sync_audit" / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Audit Qdrant Product Catalog...")
    qdrant_meta, qdrant_rows = scan_qdrant(settings)

    qdrant_codes = [row["product_code"] for row in qdrant_rows if row["product_code"]]
    qdrant_code_counter = Counter(qdrant_codes)
    qdrant_unique_codes = set(qdrant_code_counter)

    qdrant_normalized_map: dict[str, set[str]] = defaultdict(set)
    for code in qdrant_unique_codes:
        qdrant_normalized_map[normalized_code(code)].add(code)

    logger.info(
        "Qdrant scanned rows=%s unique_codes=%s collection_points=%s",
        len(qdrant_rows),
        len(qdrant_unique_codes),
        qdrant_meta["points_count"],
    )

    logger.info("Audit Shopify ACTIVE...")
    shopify_rows = scan_shopify()

    first_code_counter = Counter(
        row["current_rule_code"]
        for row in shopify_rows
        if row["current_rule_code"]
    )

    sku_to_products: dict[str, list[dict]] = defaultdict(list)
    for row in shopify_rows:
        for sku in row["variant_skus"]:
            sku_to_products[sku].append(row)

    matched_current: list[dict] = []
    matched_any_variant: list[dict] = []
    unmatched: list[dict] = []
    no_sku: list[dict] = []
    case_only_candidates: list[dict] = []

    matched_current_codes: set[str] = set()
    active_any_skus: set[str] = set()
    active_first_codes: set[str] = set()

    for row in shopify_rows:
        current_code = row["current_rule_code"]
        variant_skus = row["variant_skus"]
        active_any_skus.update(variant_skus)

        if current_code:
            active_first_codes.add(current_code)
        else:
            no_sku.append(row)

        exact_current = bool(current_code and current_code in qdrant_unique_codes)
        any_variant_matches = sorted(
            sku for sku in variant_skus if sku in qdrant_unique_codes
        )

        if exact_current:
            matched_current_codes.add(current_code)
            matched_current.append(
                {
                    **row,
                    "matched_code": current_code,
                    "variant_skus_text": " | ".join(variant_skus),
                }
            )

        if any_variant_matches:
            matched_any_variant.append(
                {
                    **row,
                    "matched_codes": " | ".join(any_variant_matches),
                    "variant_skus_text": " | ".join(variant_skus),
                }
            )
        else:
            unmatched.append(
                {
                    **row,
                    "variant_skus_text": " | ".join(variant_skus),
                }
            )

        if current_code and not exact_current:
            normalized = normalized_code(current_code)
            candidates = sorted(qdrant_normalized_map.get(normalized) or [])
            if candidates:
                case_only_candidates.append(
                    {
                        **row,
                        "qdrant_candidates": " | ".join(candidates),
                        "variant_skus_text": " | ".join(variant_skus),
                    }
                )

    qdrant_duplicate_rows = [
        {
            "product_code": code,
            "qdrant_point_count": count,
        }
        for code, count in sorted(qdrant_code_counter.items())
        if count > 1
    ]

    shopify_duplicate_first_codes = [
        {
            "sku": code,
            "shopify_product_count": count,
            "titles": " | ".join(
                row["title"]
                for row in shopify_rows
                if row["current_rule_code"] == code
            )[:4000],
        }
        for code, count in sorted(first_code_counter.items())
        if count > 1
    ]

    shopify_duplicate_any_skus = []
    for sku, products in sorted(sku_to_products.items()):
        unique_product_ids = {
            row["shopify_id"] or row["legacy_resource_id"] or row["handle"]
            for row in products
        }
        if len(unique_product_ids) <= 1:
            continue

        shopify_duplicate_any_skus.append(
            {
                "sku": sku,
                "shopify_product_count": len(unique_product_ids),
                "titles": " | ".join(
                    dict.fromkeys(row["title"] for row in products)
                )[:4000],
            }
        )

    qdrant_missing_from_active_first = sorted(
        qdrant_unique_codes - active_first_codes
    )
    qdrant_missing_from_active_any = sorted(
        qdrant_unique_codes - active_any_skus
    )

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "qdrant": {
            **qdrant_meta,
            "unique_product_codes": len(qdrant_unique_codes),
            "duplicate_product_codes": len(qdrant_duplicate_rows),
            "blank_product_codes": sum(
                1 for row in qdrant_rows if not row["product_code"]
            ),
        },
        "shopify_active": {
            "products": len(shopify_rows),
            "products_without_sku": len(no_sku),
            "unique_first_skus": len(active_first_codes),
            "unique_variant_skus": len(active_any_skus),
            "duplicate_first_skus": len(shopify_duplicate_first_codes),
            "duplicate_variant_skus_across_products": len(
                shopify_duplicate_any_skus
            ),
        },
        "matching": {
            "current_rule_shopify_products_matched": len(matched_current),
            "current_rule_unique_qdrant_codes_matched": len(
                matched_current_codes
            ),
            "any_variant_shopify_products_matched": len(
                matched_any_variant
            ),
            "shopify_products_unmatched_by_any_variant": len(unmatched),
            "case_only_current_rule_candidates": len(case_only_candidates),
            "qdrant_codes_not_active_as_first_sku": len(
                qdrant_missing_from_active_first
            ),
            "qdrant_codes_not_active_in_any_variant": len(
                qdrant_missing_from_active_any
            ),
        },
    }

    # JSON summary + machine-readable detail.
    report = {
        "summary": summary,
        "qdrant_duplicate_codes": qdrant_duplicate_rows,
        "shopify_duplicate_first_skus": shopify_duplicate_first_codes,
        "shopify_duplicate_any_variant_skus": shopify_duplicate_any_skus,
        "qdrant_codes_not_active_in_any_variant": qdrant_missing_from_active_any,
    }
    (output_dir / "audit_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    write_csv(
        output_dir / "matched_current_rule.csv",
        matched_current,
        [
            "shopify_id",
            "legacy_resource_id",
            "title",
            "handle",
            "updated_at",
            "current_rule_code",
            "matched_code",
            "variant_skus_text",
        ],
    )
    write_csv(
        output_dir / "matched_any_variant.csv",
        matched_any_variant,
        [
            "shopify_id",
            "legacy_resource_id",
            "title",
            "handle",
            "updated_at",
            "current_rule_code",
            "matched_codes",
            "variant_skus_text",
        ],
    )
    write_csv(
        output_dir / "unmatched_shopify.csv",
        unmatched,
        [
            "shopify_id",
            "legacy_resource_id",
            "title",
            "handle",
            "updated_at",
            "current_rule_code",
            "variant_skus_text",
        ],
    )
    write_csv(
        output_dir / "shopify_without_sku.csv",
        [
            {**row, "variant_skus_text": " | ".join(row["variant_skus"])}
            for row in no_sku
        ],
        [
            "shopify_id",
            "legacy_resource_id",
            "title",
            "handle",
            "updated_at",
            "current_rule_code",
            "variant_skus_text",
        ],
    )
    write_csv(
        output_dir / "shopify_duplicate_first_skus.csv",
        shopify_duplicate_first_codes,
        ["sku", "shopify_product_count", "titles"],
    )
    write_csv(
        output_dir / "shopify_duplicate_variant_skus.csv",
        shopify_duplicate_any_skus,
        ["sku", "shopify_product_count", "titles"],
    )
    write_csv(
        output_dir / "qdrant_duplicate_codes.csv",
        qdrant_duplicate_rows,
        ["product_code", "qdrant_point_count"],
    )
    write_csv(
        output_dir / "qdrant_not_active_any_variant.csv",
        [{"product_code": code} for code in qdrant_missing_from_active_any],
        ["product_code"],
    )
    write_csv(
        output_dir / "case_only_candidates.csv",
        case_only_candidates,
        [
            "shopify_id",
            "legacy_resource_id",
            "title",
            "handle",
            "current_rule_code",
            "qdrant_candidates",
            "variant_skus_text",
        ],
    )

    print()
    print("=" * 68)
    print("PRODUCT SYNC AUDIT - PHASE 1.5")
    print("=" * 68)
    print(f"Qdrant collection points          : {qdrant_meta['points_count']}")
    print(f"Qdrant rows scanned               : {len(qdrant_rows)}")
    print(f"Qdrant unique product_code        : {len(qdrant_unique_codes)}")
    print(f"Qdrant duplicate product_code     : {len(qdrant_duplicate_rows)}")
    print("-" * 68)
    print(f"Shopify ACTIVE products           : {len(shopify_rows)}")
    print(f"Shopify products without SKU      : {len(no_sku)}")
    print(f"Shopify unique first SKU          : {len(active_first_codes)}")
    print(f"Shopify duplicate first SKU       : {len(shopify_duplicate_first_codes)}")
    print("-" * 68)
    print(f"Matched products - current rule   : {len(matched_current)}")
    print(f"Matched UNIQUE Qdrant codes       : {len(matched_current_codes)}")
    print(f"Matched products - any variant    : {len(matched_any_variant)}")
    print(f"Unmatched by any variant          : {len(unmatched)}")
    print(f"Qdrant not ACTIVE in any variant  : {len(qdrant_missing_from_active_any)}")
    print(f"Case-only current-code candidates : {len(case_only_candidates)}")
    print("=" * 68)
    print(f"Report folder: {output_dir}")
    print("Quan trọng: Phase 1.5 chỉ audit, KHÔNG ghi Qdrant.")
    print()


if __name__ == "__main__":
    main()
