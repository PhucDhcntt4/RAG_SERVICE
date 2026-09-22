from __future__ import annotations

import base64
import json
import re
import unicodedata
from urllib.parse import quote

import httpx


class ProductRepository:
    def __init__(self, settings):
        self.base_url = settings.qdrant_url.rstrip("/")
        self.catalog_collection = settings.product_catalog_collection
        self.image_collection = settings.product_image_collection

        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=settings.qdrant_timeout_seconds,
        )

    def close(self):
        self.client.close()

    def _collection_path(self, collection: str) -> str:
        return "/collections/" + quote(collection, safe="")

    def _request(self, method: str, path: str, **kwargs):
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()

        body = response.json()

        if body.get("status") not in (None, "ok"):
            raise RuntimeError("Qdrant trả về trạng thái không hợp lệ")

        return body.get("result")

    # ---------------------------------------------------------
    # Cursor
    # ---------------------------------------------------------

    @staticmethod
    def _encode_cursor(value):
        if value is None:
            return None

        raw = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        return (
            base64.urlsafe_b64encode(raw)
            .decode("ascii")
            .rstrip("=")
        )

    @staticmethod
    def _decode_cursor(value):
        if not value:
            return None

        padding = "=" * (-len(value) % 4)

        raw = base64.urlsafe_b64decode(
            (value + padding).encode("ascii")
        )

        return json.loads(raw.decode("utf-8"))

    # ---------------------------------------------------------
    # Collections
    # ---------------------------------------------------------

    def collection_status(self):
        catalog = self._request(
            "GET",
            self._collection_path(self.catalog_collection),
        )

        images = self._request(
            "GET",
            self._collection_path(self.image_collection),
        )

        return {
            "catalog": {
                "name": self.catalog_collection,
                "status": catalog.get("status"),
                "points_count": catalog.get("points_count"),
                "indexed_vectors_count": catalog.get(
                    "indexed_vectors_count"
                ),
            },
            "images": {
                "name": self.image_collection,
                "status": images.get("status"),
                "points_count": images.get("points_count"),
                "indexed_vectors_count": images.get(
                    "indexed_vectors_count"
                ),
            },
        }

    # ---------------------------------------------------------
    # Products
    # ---------------------------------------------------------

    @staticmethod
    def _product_summary(point):
        payload = point.get("payload") or {}

        summary = payload.get("summary") or {}
        public_info = payload.get("public_info") or {}

        image_urls = public_info.get("image_urls") or []

        if not image_urls:
            for image in payload.get("images") or []:
                source_url = image.get("source_url")
                if source_url:
                    image_urls.append(source_url)
                    break

        product_code = (
            payload.get("product_code")
            or summary.get("product_code")
        )

        title = (
            payload.get("title")
            or summary.get("title")
            or public_info.get("product_name")
        )

        colors = summary.get("colors")

        if not colors:
            colors = public_info.get("colors") or []

        if isinstance(colors, list):
            colors = ", ".join(str(x) for x in colors)

        return {
            "point_id": point.get("id"),
            "product_code": product_code,
            "title": title,
            "product_type": (
                payload.get("product_type")
                or summary.get("product_type")
            ),
            "vendor": payload.get("vendor"),
            "status": (
                payload.get("status")
                or summary.get("status")
            ),
            "colors": colors,
            "variant_count": summary.get("variant_count", 0),
            "image_count": summary.get("image_count", 0),
            "embedding_count": summary.get(
                "embedding_count",
                0,
            ),
            "ai_ready": bool(summary.get("ai_ready")),
            "updated_at": (
                payload.get("updated_at")
                or summary.get("updated_at")
            ),
            "image_url": image_urls[0] if image_urls else None,
        }

    @staticmethod
    def _normalize_search_text(value) -> str:
        text = unicodedata.normalize("NFD", str(value or "").casefold())
        text = "".join(
            character
            for character in text
            if unicodedata.category(character) != "Mn"
        )
        text = text.replace("đ", "d")
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _matches_search(cls, payload: dict, query: str) -> bool:
        detail_product = ((payload.get("detail") or {}).get("product") or {})
        variants = payload.get("variants") or []
        aliases = payload.get("aliases") or []
        searchable_values = [
            payload.get("product_code"),
            payload.get("title"),
            payload.get("product_type"),
            payload.get("vendor"),
            detail_product.get("handle"),
            *(payload.get("variant_skus") or []),
            *(row.get("sku") for row in variants if isinstance(row, dict)),
            *(row.get("alias") for row in aliases if isinstance(row, dict)),
        ]
        haystack = cls._normalize_search_text(" ".join(
            str(value) for value in searchable_values if value
        ))
        terms = cls._normalize_search_text(query).split()
        return bool(terms) and all(term in haystack for term in terms)

    def list_products(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        query: str | None = None,
    ):
        cursor_value = self._decode_cursor(cursor)
        if isinstance(cursor_value, dict):
            offset = cursor_value.get("offset")
            exclude_point_id = cursor_value.get("exclude_point_id")
        else:
            offset = cursor_value
            exclude_point_id = None
        products = []
        normalized_query = self._normalize_search_text(query)
        next_page_offset = None

        while len(products) < limit:
            # Search scans larger batches. When a page fills in the middle of a
            # batch, the last returned point ID becomes the next Qdrant offset,
            # so later matching rows are not skipped.
            body = {
                "limit": 100 if normalized_query else limit - len(products),
                "with_payload": True,
                "with_vector": False,
            }

            if offset is not None:
                body["offset"] = offset

            result = self._request(
                "POST",
                self._collection_path(
                    self.catalog_collection
                ) + "/points/scroll",
                json=body,
            )

            points = result.get("points", [])
            page_filled = False
            for index, point in enumerate(points):
                if exclude_point_id is not None and point.get("id") == exclude_point_id:
                    continue

                payload = point.get("payload") or {}
                if normalized_query and not self._matches_search(
                    payload,
                    normalized_query,
                ):
                    continue

                row = self._product_summary(point)
                if row["product_code"]:
                    products.append(row)

                if len(products) >= limit:
                    has_more_points = (
                        index < len(points) - 1
                        or result.get("next_page_offset") is not None
                    )
                    if normalized_query and has_more_points:
                        next_page_offset = {
                            "offset": point.get("id"),
                            "exclude_point_id": point.get("id"),
                        }
                    else:
                        next_page_offset = (
                            result.get("next_page_offset")
                            if has_more_points
                            else None
                        )
                    page_filled = True
                    break

            if page_filled:
                break

            next_page_offset = result.get("next_page_offset")
            if next_page_offset is None:
                break
            offset = next_page_offset
            exclude_point_id = None

        return {
            "products": products,
            "next_cursor": self._encode_cursor(
                next_page_offset
            ),
        }

    def get_product(self, product_code: str):
        body = {
            "limit": 1,
            "with_payload": True,
            "with_vector": False,
            "filter": {
                "must": [
                    {
                        "key": "product_code",
                        "match": {
                            "value": product_code,
                        },
                    }
                ]
            },
        }

        result = self._request(
            "POST",
            self._collection_path(
                self.catalog_collection
            ) + "/points/scroll",
            json=body,
        )

        points = result.get("points", [])

        if not points:
            return None

        point = points[0]

        return {
            "point_id": point.get("id"),
            "product": point.get("payload") or {},
        }
