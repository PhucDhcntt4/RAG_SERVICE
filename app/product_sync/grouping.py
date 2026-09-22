from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.product_sync.shopify_client import (
    product_code_from_shopify,
    variant_skus_from_shopify,
)

COLOR_OPTION_NAMES = {
    "color",
    "colour",
    "màu",
    "mau",
    "màu sắc",
    "mau sac",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = _clean(value)
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _extract_colors(product: dict) -> list[str]:
    colors: list[str] = []
    variants = ((product.get("variants") or {}).get("nodes") or [])

    for variant in variants:
        for option in variant.get("selectedOptions") or []:
            name = _clean(option.get("name")).casefold()
            value = _clean(option.get("value"))
            if name in COLOR_OPTION_NAMES and value:
                colors.append(value)

    return _dedupe_keep_order(colors)


def _extract_images(product: dict) -> list[dict]:
    images: list[dict] = []
    media_nodes = ((product.get("media") or {}).get("nodes") or [])

    for media in media_nodes:
        image = media.get("image") or {}
        url = _clean(image.get("url"))
        if not url:
            continue

        images.append(
            {
                "media_id": _clean(media.get("id")),
                "image_id": _clean(image.get("id")),
                "url": url,
                "alt_text": _clean(image.get("altText")),
                "width": image.get("width"),
                "height": image.get("height"),
            }
        )

    return images


def _extract_variants(product: dict) -> list[dict]:
    rows: list[dict] = []
    variants = ((product.get("variants") or {}).get("nodes") or [])

    for variant in variants:
        selected_options = variant.get("selectedOptions") or []
        options = {
            _clean(option.get("name")): _clean(option.get("value"))
            for option in selected_options
            if _clean(option.get("name"))
        }

        color = ""
        size = ""

        for name, value in options.items():
            folded = name.casefold()
            if folded in COLOR_OPTION_NAMES:
                color = value
            elif folded in {"size", "kích thước", "kich thuoc"}:
                size = value

        rows.append(
            {
                "shopify_variant_id": _clean(variant.get("id")),
                "legacy_variant_id": _clean(variant.get("legacyResourceId")),
                "sku": _clean(variant.get("sku")),
                "barcode": _clean(variant.get("barcode")),
                "title": _clean(variant.get("title")),
                "price": _clean(variant.get("price")),
                "compare_at_price": _clean(variant.get("compareAtPrice")),
                "inventory_quantity": variant.get("inventoryQuantity"),
                "available": bool(variant.get("availableForSale")),
                "color": color,
                "size": size,
                "options": options,
            }
        )

    return rows


@dataclass
class ProductGroup:
    product_code: str
    source_products: list[dict] = field(default_factory=list)

    def add(self, product: dict):
        self.source_products.append(product)

    @property
    def source_product_count(self) -> int:
        return len(self.source_products)

    @property
    def titles(self) -> list[str]:
        return _dedupe_keep_order(
            [_clean(product.get("title")) for product in self.source_products]
        )

    @property
    def handles(self) -> list[str]:
        return _dedupe_keep_order(
            [_clean(product.get("handle")) for product in self.source_products]
        )

    @property
    def vendors(self) -> list[str]:
        return _dedupe_keep_order(
            [_clean(product.get("vendor")) for product in self.source_products]
        )

    @property
    def product_types(self) -> list[str]:
        return _dedupe_keep_order(
            [_clean(product.get("productType")) for product in self.source_products]
        )

    @property
    def colors(self) -> list[str]:
        colors: list[str] = []
        for product in self.source_products:
            colors.extend(_extract_colors(product))
        return _dedupe_keep_order(colors)

    @property
    def variants(self) -> list[dict]:
        rows: list[dict] = []
        for product in self.source_products:
            rows.extend(_extract_variants(product))
        return rows

    @property
    def images(self) -> list[dict]:
        # Deduplicate by URL because Shopify media IDs can differ while the
        # actual image is the same.
        seen_urls: set[str] = set()
        rows: list[dict] = []

        for product in self.source_products:
            product_colors = _extract_colors(product)
            default_color = product_colors[0] if len(product_colors) == 1 else ""

            for image in _extract_images(product):
                url = image["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                rows.append(
                    {
                        **image,
                        "color": default_color,
                        "source_product_id": _clean(product.get("id")),
                        "source_handle": _clean(product.get("handle")),
                    }
                )

        return rows

    @property
    def latest_updated_at(self) -> str:
        values = sorted(
            (
                _clean(product.get("updatedAt"))
                for product in self.source_products
                if _clean(product.get("updatedAt"))
            ),
            reverse=True,
        )
        return values[0] if values else ""

    def summary(self) -> dict:
        variants = self.variants
        images = self.images

        return {
            "product_code": self.product_code,
            "source_product_count": self.source_product_count,
            "colors": self.colors,
            "color_count": len(self.colors),
            "variant_count": len(variants),
            "image_count": len(images),
            "titles": self.titles,
            "handles": self.handles,
            "vendors": self.vendors,
            "product_types": self.product_types,
            "latest_updated_at": self.latest_updated_at,
            "variant_skus": _dedupe_keep_order(
                [row["sku"] for row in variants if row["sku"]]
            ),
        }

    def preview_payload(self) -> dict:
        """Preview normalized payload for inspection only.

        This is intentionally not yet the final Qdrant write schema.
        Phase 2B will map this into the current catalog/image payload schema.
        """
        return {
            **self.summary(),
            "variants": self.variants,
            "images": self.images,
            "source_products": [
                {
                    "id": _clean(product.get("id")),
                    "legacy_resource_id": _clean(product.get("legacyResourceId")),
                    "title": _clean(product.get("title")),
                    "handle": _clean(product.get("handle")),
                    "status": _clean(product.get("status")),
                    "updated_at": _clean(product.get("updatedAt")),
                    "online_store_url": _clean(product.get("onlineStoreUrl")),
                    "colors": _extract_colors(product),
                    "variant_skus": variant_skus_from_shopify(product),
                }
                for product in self.source_products
            ],
        }


def group_shopify_products(
    products: list[dict],
) -> tuple[dict[str, ProductGroup], list[dict]]:
    groups: dict[str, ProductGroup] = {}
    skipped_without_code: list[dict] = []

    for product in products:
        code = product_code_from_shopify(product)

        if not code:
            skipped_without_code.append(product)
            continue

        group = groups.get(code)
        if group is None:
            group = ProductGroup(product_code=code)
            groups[code] = group

        group.add(product)

    return groups, skipped_without_code
