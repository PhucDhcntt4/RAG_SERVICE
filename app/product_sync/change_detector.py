from __future__ import annotations

from typing import Any


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _variant_snapshot(payload: dict) -> list[dict]:
    rows = []
    for raw in payload.get("variants") or []:
        row = raw or {}
        rows.append(
            {
                "external_id": _clean(row.get("external_id")),
                "sku": _clean(row.get("sku")),
                "barcode": _clean(row.get("barcode")),
                "title": _clean(row.get("variant_title")),
                "color": _clean(row.get("color")),
                "size": _clean(row.get("size")),
                "price": row.get("price"),
                "compare_at_price": row.get("compare_at_price"),
            }
        )
    rows.sort(
        key=lambda row: (
            row["external_id"],
            row["sku"],
            row["color"],
            row["size"],
        )
    )
    return rows


def _image_snapshot(payload: dict) -> list[dict]:
    rows = []
    for raw in payload.get("images") or []:
        row = raw or {}
        rows.append(
            {
                "external_id": _clean(row.get("external_id")),
                "color": _clean(row.get("color")),
                "source_url": _clean(row.get("source_url")),
                "alt_text": _clean(row.get("alt_text")),
                "image_order": row.get("image_order"),
            }
        )
    rows.sort(
        key=lambda row: (
            row["color"],
            row["image_order"] or 0,
            row["external_id"],
            row["source_url"],
        )
    )
    return rows


def product_snapshot(payload: dict | None) -> dict:
    value = payload or {}
    return {
        "title": _clean(value.get("title")),
        "vendor": _clean(value.get("vendor")),
        "product_type": _clean(value.get("product_type")),
        "status": _clean(value.get("status")).upper(),
        "description": _clean(value.get("description")),
        "material": _clean(value.get("material")),
        "sole": _clean(value.get("sole")),
        "height": _clean(value.get("height")),
        "variants": _variant_snapshot(value),
        "images": _image_snapshot(value),
    }


def build_product_change(
    *,
    product_code: str,
    old_payload: dict | None,
    new_payload: dict,
    change_type: str | None = None,
) -> dict | None:
    before = product_snapshot(old_payload)
    after = product_snapshot(new_payload)

    if old_payload is None:
        resolved_type = "CREATED"
        changes = {
            "product": {
                "before": None,
                "after": {
                    "title": after["title"],
                    "status": after["status"],
                    "product_type": after["product_type"],
                    "variant_count": len(after["variants"]),
                    "image_count": len(after["images"]),
                },
            }
        }
    else:
        changes = {
            field: {"before": before[field], "after": after[field]}
            for field in after
            if before[field] != after[field]
        }
        if not changes:
            return None
        if change_type:
            resolved_type = change_type
        elif set(changes) == {"status"}:
            resolved_type = "STATUS_CHANGED"
        else:
            resolved_type = "UPDATED"

    return {
        "product_code": _clean(product_code),
        "product_title": after["title"] or before["title"],
        "change_type": resolved_type,
        "changes": changes,
    }
