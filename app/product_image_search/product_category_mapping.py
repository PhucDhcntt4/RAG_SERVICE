"""Load the editable Gemini-to-catalog product category mapping."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any


CATEGORY_MAPPING_PATH = (
    Path(__file__).resolve().parent / "product_category_mapping.json"
)


class ProductCategoryMappingError(RuntimeError):
    """Raised when the product category mapping is missing or invalid."""


@lru_cache(maxsize=1)
def load_product_category_mapping() -> dict[str, tuple[str, ...]]:
    """Return a validated and de-duplicated category mapping."""

    try:
        raw: Any = json.loads(CATEGORY_MAPPING_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProductCategoryMappingError(
            "Không thể đọc cấu hình nhóm sản phẩm."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ProductCategoryMappingError(
            "Cấu hình nhóm sản phẩm không phải JSON hợp lệ."
        ) from exc

    if not isinstance(raw, dict):
        raise ProductCategoryMappingError(
            "Cấu hình nhóm sản phẩm phải là object JSON."
        )

    mapping: dict[str, tuple[str, ...]] = {}
    for raw_category, raw_product_types in raw.items():
        category = str(raw_category).strip().lower()
        if not category or not isinstance(raw_product_types, list):
            raise ProductCategoryMappingError(
                "Mỗi nhóm sản phẩm phải có tên và danh sách product_type."
            )

        product_types: list[str] = []
        seen: set[str] = set()
        for value in raw_product_types:
            normalized = str(value).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                product_types.append(normalized)
        mapping[category] = tuple(product_types)

    return mapping

