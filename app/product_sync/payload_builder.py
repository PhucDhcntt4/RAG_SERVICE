from __future__ import annotations

import mimetypes
import re
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from app.product_sync.grouping import ProductGroup


SPEC_PATTERNS = {
    "material": [
        r"(?:^|\n|[-•]\s*)Chất\s*liệu\s*:\s*([^\n\r]+)",
        r"(?:^|\n|[-•]\s*)Chat\s*lieu\s*:\s*([^\n\r]+)",
    ],
    "sole": [
        r"(?:^|\n|[-•]\s*)Đế\s*:\s*([^\n\r]+)",
        r"(?:^|\n|[-•]\s*)De\s*:\s*([^\n\r]+)",
    ],
    "height": [
        r"(?:^|\n|[-•]\s*)(?:Độ\s*cao|Chiều\s*cao)\s*:\s*([^\n\r]+)",
    ],
}


def clean(value: Any) -> str:
    return str(value or "").strip()


def normalize_vi(value: str) -> str:
    value = clean(value).casefold()
    value = unicodedata.normalize("NFD", value)
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = value.replace("đ", "d")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def number(value: Any):
    text = clean(value)
    if not text:
        return None
    try:
        if "." in text:
            as_float = float(text)
            if as_float.is_integer():
                return int(as_float)
            return as_float
        return int(text)
    except (TypeError, ValueError):
        return None


def dedupe(values):
    result = []
    seen = set()
    for value in values:
        marker = repr(value)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def extract_spec(description: str, key: str):
    text = clean(description)
    for pattern in SPEC_PATTERNS.get(key, []):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = clean(match.group(1))
            # Stop before another inline "- X:" spec when Shopify flattened HTML.
            value = re.split(r"\s+-\s+[A-Za-zÀ-ỹĐđ][^:]{0,40}:", value, maxsplit=1)[0]
            return clean(value)
    return None


def public_description(description: str) -> str:
    text = clean(description)
    if not text:
        return ""

    markers = [
        "- Mã sản phẩm:",
        "- Mã SP:",
        "- Loại sản phẩm:",
        "- Màu sắc:",
        "- Chất liệu:",
    ]
    positions = [text.find(marker) for marker in markers if text.find(marker) >= 0]
    if positions:
        return text[: min(positions)].strip()
    return text


def guess_mime_type(url: str) -> str:
    path = urlparse(url).path
    mime, _ = mimetypes.guess_type(path)
    return mime or "image/jpeg"


def choose_primary_source(group: ProductGroup) -> dict:
    # Latest updated source is the safest canonical source for common fields.
    ordered = sorted(
        group.source_products,
        key=lambda item: clean(item.get("updatedAt")),
        reverse=True,
    )
    return ordered[0] if ordered else {}


def existing_image_map(existing_payload: dict | None) -> dict[str, dict]:
    result = {}
    for image in (existing_payload or {}).get("images") or []:
        url = clean(image.get("source_url"))
        if url:
            result[url] = image
    return result


def build_variants(group: ProductGroup) -> list[dict]:
    rows = []
    seen = set()

    for raw in group.variants:
        key = (
            clean(raw.get("shopify_variant_id")),
            clean(raw.get("color")),
            clean(raw.get("size")),
        )
        if key in seen:
            continue
        seen.add(key)

        rows.append(
            {
                "external_id": clean(raw.get("shopify_variant_id")),
                "legacy_id": clean(raw.get("legacy_variant_id")) or None,
                "sku": clean(raw.get("sku")) or group.product_code,
                "barcode": clean(raw.get("barcode")) or None,
                "variant_title": clean(raw.get("title")),
                "color": clean(raw.get("color")),
                "color_normalized": normalize_vi(raw.get("color")),
                "size": clean(raw.get("size")),
                "price": number(raw.get("price")),
                "compare_at_price": number(raw.get("compare_at_price")),
                "inventory_quantity": raw.get("inventory_quantity"),
                "available": bool(raw.get("available")),
            }
        )

    return rows


def build_images(
    group: ProductGroup,
    existing_payload: dict | None = None,
) -> tuple[list[dict], dict]:
    old_by_url = existing_image_map(existing_payload)
    new_images = []
    new_urls = set()

    for index, raw in enumerate(group.images, start=1):
        url = clean(raw.get("url"))
        if not url:
            continue

        new_urls.add(url)
        old = old_by_url.get(url) or {}

        new_images.append(
            {
                "external_id": (
                    clean(raw.get("image_id"))
                    or clean(raw.get("media_id"))
                    or clean(old.get("external_id"))
                ),
                "color": clean(raw.get("color")),
                "color_normalized": normalize_vi(raw.get("color")),
                "source_url": url,
                "local_path": clean(old.get("local_path")),
                "alt_text": clean(raw.get("alt_text")),
                "mime_type": clean(old.get("mime_type")) or guess_mime_type(url),
                "width": raw.get("width"),
                "height": raw.get("height"),
                "image_order": index,
                "is_featured": bool(old.get("is_featured", False)),
                "checksum": clean(old.get("checksum")),
                "is_active": True,
                # Existing exact URL can reuse its embedding state.
                "embedded": bool(old.get("embedded", False)),
            }
        )

    old_urls = set(old_by_url)
    added_urls = sorted(new_urls - old_urls)
    removed_urls = sorted(old_urls - new_urls)
    unchanged_urls = sorted(new_urls & old_urls)
    embedded_unchanged = sum(
        1
        for url in unchanged_urls
        if bool((old_by_url.get(url) or {}).get("embedded"))
    )

    diff = {
        "added_urls": added_urls,
        "removed_urls": removed_urls,
        "unchanged_urls": unchanged_urls,
        "added_count": len(added_urls),
        "removed_count": len(removed_urls),
        "unchanged_count": len(unchanged_urls),
        "embedded_unchanged_count": embedded_unchanged,
        "requires_image_embedding": bool(added_urls),
    }
    return new_images, diff


def build_catalog_payload(
    group: ProductGroup,
    existing_payload: dict | None = None,
) -> tuple[dict, dict]:
    primary = choose_primary_source(group)
    description = clean(primary.get("description"))
    title = clean(primary.get("title")) or group.product_code
    product_type = clean(primary.get("productType"))
    vendor = clean(primary.get("vendor"))

    material = extract_spec(description, "material")
    sole = extract_spec(description, "sole")
    height = extract_spec(description, "height")

    variants = build_variants(group)
    images, image_diff = build_images(group, existing_payload)
    inventory_synced_at = datetime.now(UTC).isoformat(timespec="seconds")

    colors = dedupe(
        [row["color"] for row in variants if row["color"]]
        + [color for color in group.colors if color]
    )
    sizes = dedupe(
        [row["size"] for row in variants if row["size"]]
    )

    prices = dedupe(
        [row["price"] for row in variants if row["price"] is not None]
    )

    variant_prices = [
        {
            "color": row["color"],
            "size": row["size"],
            "price": row["price"],
            "available": row["available"],
        }
        for row in variants
        if row["price"] is not None
    ]

    availability_by_color = {}
    for color in colors:
        matching = [row for row in variants if row["color"] == color]
        availability_by_color[color] = {
            "available": any(row["available"] for row in matching),
            "available_sizes": dedupe(
                [row["size"] for row in matching if row["size"] and row["available"]]
            ),
        }

    image_urls = [row["source_url"] for row in images]
    image_urls_by_color = defaultdict(list)
    for row in images:
        if row["color"]:
            image_urls_by_color[row["color"]].append(row["source_url"])

    all_images_embedded = bool(images) and all(row["embedded"] for row in images)
    existing_status = clean((existing_payload or {}).get("status"))
    catalog_exists = bool(existing_payload)

    # In Phase 2B this is preview state. New catalog/image embeddings are not
    # generated yet, so new/changed products must not be marked AI-ready.
    ai_ready_preview = (
        catalog_exists
        and existing_status == "ACTIVE"
        and not image_diff["requires_image_embedding"]
        and not image_diff["removed_count"]
        and all_images_embedded
    )

    aliases = [
        {"alias": group.product_code, "alias_type": "shopify"},
    ]
    if product_type:
        aliases.append({"alias": product_type, "alias_type": "shopify"})
    if title:
        aliases.append({"alias": title, "alias_type": "shopify"})

    payload = {
        "product_code": group.product_code,
        "title": title,
        "product_type": product_type,
        "description": description,
        "vendor": vendor,
        "material": material,
        "sole": sole,
        "height": height,
        "status": "ACTIVE",
        "updated_at": group.latest_updated_at,
        "inventory_source": "shopify",
        "inventory_synced_at": inventory_synced_at,
        "inventory_sync_complete": True,
        "inventory_missing_variants": 0,
        "variant_skus": dedupe([row["sku"] for row in variants if row["sku"]]),
        "public_info": {
            "product_code": group.product_code,
            "product_name": title,
            "product_type": product_type,
            "description": public_description(description),
            "material": material,
            "sole": sole,
            "height": height,
            "status": "ACTIVE",
            "inventory_synced_at": inventory_synced_at,
            "inventory_sync_complete": True,
            "prices": prices,
            "variant_prices": variant_prices,
            "colors": colors,
            "available_sizes": sizes,
            "availability_by_color": availability_by_color,
            "image_urls": image_urls,
            "image_urls_by_color": dict(image_urls_by_color),
        },
        "summary": {
            "product_code": group.product_code,
            "title": title,
            "product_type": product_type,
            "status": "ACTIVE",
            "updated_at": group.latest_updated_at,
            "inventory_synced_at": inventory_synced_at,
            "inventory_sync_complete": True,
            "inventory_missing_variants": 0,
            "available_variant_count": sum(1 for row in variants if row["available"]),
            "variant_count": len(variants),
            "image_count": len(images),
            "local_image_count": sum(1 for row in images if row["local_path"]),
            "embedding_count": sum(1 for row in images if row["embedded"]),
            "colors": ", ".join(colors),
            "ai_ready": ai_ready_preview,
        },
        "variants": variants,
        "images": images,
        "detail": {
            "product": {
                "product_code": group.product_code,
                "title": title,
                "handle": clean(primary.get("handle")),
                "vendor": vendor,
                "product_type": product_type,
                "description": description,
                "material": material,
                "sole": sole,
                "height": height,
                "status": "ACTIVE",
                "online_store_url": clean(primary.get("onlineStoreUrl")),
                "source_updated_at": group.latest_updated_at,
            },
            "variants": variants,
            "images": images,
        },
        "attributes": (
            [{"attribute_key": "material", "attribute_value": material}]
            if material
            else []
        ),
        "aliases": aliases,
        "embedding_model": "ViT-B-32",
        "embedding_pretrained": "laion2b_s34b_b79k",
        "kind": "bot_product_catalog",
    }

    diff = {
        **image_diff,
        "catalog_exists": catalog_exists,
        "source_product_count": group.source_product_count,
        "requires_catalog_embedding": True,  # Phase 2C will optimize by fingerprint.
    }
    return payload, diff


def inactive_payload(
    existing_payload: dict,
    status: str = "INACTIVE",
) -> dict:
    status = str(status or "INACTIVE").strip().upper() or "INACTIVE"
    payload = dict(existing_payload)
    payload["status"] = status

    public_info = dict(payload.get("public_info") or {})
    public_info["status"] = status
    payload["public_info"] = public_info

    summary = dict(payload.get("summary") or {})
    summary["status"] = status
    summary["ai_ready"] = False
    payload["summary"] = summary

    detail = dict(payload.get("detail") or {})
    detail_product = dict(detail.get("product") or {})
    detail_product["status"] = status
    detail["product"] = detail_product
    payload["detail"] = detail

    return payload
