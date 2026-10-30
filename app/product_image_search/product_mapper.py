"""Map the RAG Server product payload to the existing Client response model."""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import urlparse

from app.product_image_search.models import ProductSource, ProductVariant


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFD", str(value or "").casefold())
    text = "".join(
        character
        for character in text
        if unicodedata.category(character) != "Mn"
    )
    text = text.replace("đ", "d")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_http_url(value: Any) -> str | None:
    if not value:
        return None
    url = str(value).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


class ProductMapper:
    @staticmethod
    def payload(detail: dict) -> dict:
        product = detail.get("product")
        return product if isinstance(product, dict) else detail

    @staticmethod
    def to_source(
        detail: dict,
        citation: str,
        query: str = "",
    ) -> ProductSource:
        payload = ProductMapper.payload(detail)
        public_info = payload.get("public_info") or {}
        detail_product = (payload.get("detail") or {}).get("product") or {}
        images = payload.get("images") or []
        raw_variants = [
            raw
            for raw in payload.get("variants") or []
            if isinstance(raw, dict)
        ]
        image_urls_by_color = public_info.get("image_urls_by_color") or {}
        color_names = list(dict.fromkeys(
            [
                str(color).strip()
                for color in image_urls_by_color
                if str(color).strip()
            ]
            + [
                str(raw.get("color") or "").strip()
                for raw in raw_variants
                if str(raw.get("color") or "").strip()
            ]
        ))
        normalized_query = f" {normalize_text(query)} "
        matched_color = next(
            (
                color
                for color in color_names
                if f" {normalize_text(color)} " in normalized_query
            ),
            "",
        )

        selected_image_values = public_info.get("image_urls") or []
        if matched_color and image_urls_by_color:
            selected_image_values = next(
                (
                    values
                    for color, values in image_urls_by_color.items()
                    if normalize_text(color) == normalize_text(matched_color)
                ),
                selected_image_values,
            )
        image_urls = list(dict.fromkeys(
            url
            for url in (
                safe_http_url(value)
                for value in selected_image_values
            )
            if url
        ))
        image_url = next(iter(image_urls), None)
        if image_url is None:
            image_url = next(
                (
                    safe_http_url(image.get("source_url"))
                    for image in images
                    if isinstance(image, dict) and safe_http_url(image.get("source_url"))
                ),
                None,
            )

        variants: list[ProductVariant] = []
        for raw in raw_variants:
            if (
                matched_color
                and normalize_text(raw.get("color"))
                != normalize_text(matched_color)
            ):
                continue
            variants.append(
                ProductVariant(
                    sku=str(raw.get("sku") or ""),
                    barcode=str(raw.get("barcode") or ""),
                    external_id=str(raw.get("external_id") or ""),
                    variant_title=str(raw.get("variant_title") or ""),
                    color=str(raw.get("color") or ""),
                    size=str(raw.get("size") or ""),
                    price=optional_float(raw.get("price")),
                    compare_at_price=optional_float(raw.get("compare_at_price")),
                    inventory_quantity=optional_int(raw.get("inventory_quantity")),
                    available=(
                        bool(raw.get("available"))
                        if raw.get("available") is not None
                        else None
                    ),
                )
            )

        known_quantities = [
            variant.inventory_quantity
            for variant in variants
            if variant.inventory_quantity is not None
        ]
        total_inventory = (
            sum(max(0, quantity) for quantity in known_quantities)
            if known_quantities else None
        )
        available_variant_count = sum(
            1
            for variant in variants
            if variant.available
            or (
                variant.inventory_quantity is not None
                and variant.inventory_quantity > 0
            )
        )
        if available_variant_count > 0:
            inventory_status = "in_stock"
        elif variants and (
            known_quantities
            or (
                all(variant.available is not None for variant in variants)
                and all(variant.available is False for variant in variants)
            )
        ):
            inventory_status = "out_of_stock"
        else:
            inventory_status = "unknown"

        prices = [
            variant.price
            for variant in variants
            if variant.price is not None
        ]
        aliases = [
            " - ".join(
                part for part in (
                    str(item.get("alias_type") or "").strip(),
                    str(item.get("alias") or "").strip(),
                ) if part
            )
            for item in payload.get("aliases") or []
            if isinstance(item, dict) and item.get("alias")
        ]
        attributes = [
            ": ".join(
                part for part in (
                    str(item.get("attribute_key") or "").strip(),
                    str(item.get("attribute_value") or "").strip(),
                ) if part
            )
            for item in payload.get("attributes") or []
            if isinstance(item, dict) and item.get("attribute_value")
        ]

        return ProductSource(
            citation=citation,
            product_code=str(
                payload.get("product_code") or public_info.get("product_code") or ""
            ),
            title=str(
                payload.get("title") or public_info.get("product_name") or "Sản phẩm"
            ),
            product_type=str(payload.get("product_type") or ""),
            vendor=str(payload.get("vendor") or ""),
            status=str(payload.get("status") or ""),
            description=str(
                payload.get("description")
                or public_info.get("description")
                or detail_product.get("description")
                or ""
            ),
            material=str(payload.get("material") or public_info.get("material") or ""),
            sole=str(payload.get("sole") or public_info.get("sole") or ""),
            height=str(payload.get("height") or public_info.get("height") or ""),
            handle=str(detail_product.get("handle") or ""),
            aliases=aliases,
            attributes=attributes,
            image_url=image_url,
            image_urls=image_urls,
            online_store_url=safe_http_url(detail_product.get("online_store_url")),
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
            published_at=payload.get("published_at"),
            new_at=payload.get("new_at"),
            minimum_price=min(prices) if prices else None,
            maximum_price=max(prices) if prices else None,
            inventory_synced_at=payload.get("inventory_synced_at"),
            inventory_status=inventory_status,
            total_inventory=total_inventory,
            available_variant_count=available_variant_count,
            variants=variants,
            matched_color=matched_color,
        )
