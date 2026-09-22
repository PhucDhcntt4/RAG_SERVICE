from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict

import httpx


CATALOG_POINT_NAMESPACE = uuid.UUID("8d9a62bb-12c7-4c66-ae2c-17df102967d3")


def catalog_point_id(product_code: str) -> str:
    return str(
        uuid.uuid5(
            CATALOG_POINT_NAMESPACE,
            f"bot_product_catalog:{product_code.strip()}",
        )
    )


def image_point_id(product_code: str, checksum: str, color: str, salt: int = 0) -> int:
    raw = f"{product_code}\0{checksum}\0{color}\0{salt}".encode("utf-8")
    value = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")
    # New sync-created image IDs live in a very high signed-64-bit range,
    # far away from the current small legacy integer IDs.
    return (1 << 62) | (value & ((1 << 62) - 1))


class ProductQdrantStore:
    def __init__(self, settings):
        self.base_url = settings.qdrant_url.rstrip("/")
        self.catalog_collection = settings.product_catalog_collection
        self.image_collection = settings.product_image_collection
        self.client = httpx.Client(timeout=settings.qdrant_timeout_seconds)

    def close(self):
        self.client.close()

    def _request(self, method: str, path: str, **kwargs):
        response = self.client.request(
            method,
            f"{self.base_url}{path}",
            **kwargs,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") not in (None, "ok"):
            raise RuntimeError(f"Qdrant request failed: {payload}")
        return payload.get("result")

    def _collection_info(self, collection: str) -> dict:
        return self._request("GET", f"/collections/{collection}") or {}

    def validate(self) -> dict:
        catalog = self._collection_info(self.catalog_collection)
        images = self._collection_info(self.image_collection)

        catalog_vectors = (
            ((catalog.get("config") or {}).get("params") or {}).get("vectors")
        )
        image_vectors = (
            ((images.get("config") or {}).get("params") or {}).get("vectors")
        )

        if not isinstance(catalog_vectors, dict) or catalog_vectors.get("size") != 1:
            raise RuntimeError(
                "Catalog collection phải dùng vector size=1 trước khi Phase 2C ghi dữ liệu."
            )
        if str(catalog_vectors.get("distance") or "").lower() != "cosine":
            raise RuntimeError("Catalog collection phải dùng Cosine distance.")

        if not isinstance(image_vectors, dict) or image_vectors.get("size") != 512:
            raise RuntimeError(
                "Image collection phải dùng vector size=512 cho ViT-B-32."
            )
        if str(image_vectors.get("distance") or "").lower() != "cosine":
            raise RuntimeError("Image collection phải dùng Cosine distance.")

        return {
            "catalog_points": int(catalog.get("points_count") or 0),
            "catalog_vector_size": 1,
            "catalog_distance": catalog_vectors.get("distance"),
            "image_points": int(images.get("points_count") or 0),
            "image_vector_size": 512,
            "image_distance": image_vectors.get("distance"),
        }

    def all_catalog(self) -> dict[str, dict]:
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

            data = self._request(
                "POST",
                f"/collections/{self.catalog_collection}/points/scroll",
                json=body,
            ) or {}

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

    def all_images_by_code(self) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = defaultdict(list)
        offset = None

        while True:
            body = {
                "limit": 256,
                "with_payload": True,
                "with_vector": False,
            }
            if offset is not None:
                body["offset"] = offset

            data = self._request(
                "POST",
                f"/collections/{self.image_collection}/points/scroll",
                json=body,
            ) or {}

            for point in data.get("points") or []:
                payload = point.get("payload") or {}
                code = str(payload.get("product_code") or "").strip()
                if code:
                    result[code].append(
                        {
                            "point_id": point.get("id"),
                            "payload": payload,
                        }
                    )

            offset = data.get("next_page_offset")
            if offset is None:
                break

        return dict(result)

    def image_point(self, point_id: int) -> dict | None:
        rows = self._request(
            "POST",
            f"/collections/{self.image_collection}/points",
            json={
                "ids": [point_id],
                "with_payload": True,
                "with_vector": False,
            },
        ) or []

        return rows[0] if rows else None

    def allocate_image_point_id(
        self,
        product_code: str,
        checksum: str,
        color: str,
    ) -> int:
        for salt in range(100):
            candidate = image_point_id(product_code, checksum, color, salt)
            existing = self.image_point(candidate)

            if existing is None:
                return candidate

            payload = existing.get("payload") or {}
            if (
                str(payload.get("product_code") or "") == product_code
                and str(payload.get("image_checksum") or "") == checksum
                and str(payload.get("color") or "") == color
            ):
                return candidate

        raise RuntimeError("Không thể cấp point ID an toàn cho image vector.")

    def upsert_catalog(
        self,
        point_id,
        payload: dict,
    ):
        self._request(
            "PUT",
            f"/collections/{self.catalog_collection}/points",
            params={"wait": "true"},
            json={
                "points": [
                    {
                        "id": point_id,
                        # Confirmed by current catalog point: vector is [1.0].
                        "vector": [1.0],
                        "payload": payload,
                    }
                ]
            },
        )

    def upsert_catalog_batch(self, rows: list[dict], chunk_size: int = 100):
        """Batch-write catalog payloads only; image collection is untouched."""
        chunk_size = min(max(int(chunk_size), 1), 256)
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start:start + chunk_size]
            self._request(
                "PUT",
                f"/collections/{self.catalog_collection}/points",
                params={"wait": "true"},
                json={
                    "points": [
                        {
                            "id": row["point_id"],
                            "vector": [1.0],
                            "payload": row["payload"],
                        }
                        for row in chunk
                    ]
                },
            )

    def upsert_image(
        self,
        point_id: int,
        vector: list[float],
        payload: dict,
    ):
        if len(vector) != 512:
            raise ValueError(f"CLIP vector dimension phải là 512, nhận {len(vector)}")

        self._request(
            "PUT",
            f"/collections/{self.image_collection}/points",
            params={"wait": "true"},
            json={
                "points": [
                    {
                        "id": point_id,
                        "vector": vector,
                        "payload": payload,
                    }
                ]
            },
        )

    def set_image_payload(self, point_id: int | str, payload: dict):
        self._request(
            "POST",
            f"/collections/{self.image_collection}/points/payload",
            params={"wait": "true"},
            json={
                "payload": payload,
                "points": [point_id],
            },
        )

    def delete_image_points(self, point_ids: list):
        point_ids = list(dict.fromkeys(point_ids))
        if not point_ids:
            return

        self._request(
            "POST",
            f"/collections/{self.image_collection}/points/delete",
            params={"wait": "true"},
            json={"points": point_ids},
        )
