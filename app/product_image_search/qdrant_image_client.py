"""Minimal Qdrant REST client for product image similarity search."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx  # type: ignore


class QdrantImageError(RuntimeError):
    pass


class QdrantImageClient:
    def __init__(
        self,
        *,
        base_url: str,
        collection: str,
        api_key: str | None,
        timeout: float = 30,
        client: httpx.AsyncClient | None = None,
    ):
        headers = {"Accept": "application/json"}
        if api_key:
            headers["api-key"] = api_key
        self._headers = headers
        self.collection = collection
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=5.0),
        )

    async def search(
        self,
        vector: list[float],
        *,
        limit: int,
        score_threshold: float | None = None,
        product_types: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        if len(vector) != 512:
            raise QdrantImageError("Vector truy vấn ảnh phải có 512 chiều.")

        collection = quote(self.collection, safe="")
        try:
            payload: dict[str, Any] = {
                "query": vector,
                "limit": limit,
                "with_payload": True,
                "with_vector": False,
            }
            if score_threshold is not None:
                payload["score_threshold"] = score_threshold
            normalized_product_types = list(
                dict.fromkeys(
                    str(value).strip()
                    for value in (product_types or [])
                    if str(value).strip()
                )
            )
            if normalized_product_types:
                payload["filter"] = {
                    "must": [
                        {
                            "key": "product_type",
                            "match": {"any": normalized_product_types},
                        }
                    ]
                }

            response = await self._client.post(
                f"/collections/{collection}/points/query",
                headers=self._headers,
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.TimeoutException as exc:
            raise QdrantImageError("Qdrant phản hồi quá thời gian.") from exc
        except httpx.HTTPStatusError as exc:
            raise QdrantImageError(
                f"Qdrant tìm kiếm ảnh thất bại (HTTP {exc.response.status_code})."
            ) from exc
        except (httpx.RequestError, ValueError) as exc:
            raise QdrantImageError("Không thể truy vấn kho vector ảnh Qdrant.") from exc

        if body.get("status") not in (None, "ok"):
            raise QdrantImageError("Qdrant trả về trạng thái không hợp lệ.")

        result = body.get("result")
        points = result.get("points") if isinstance(result, dict) else result
        if not isinstance(points, list):
            raise QdrantImageError("Response tìm kiếm ảnh của Qdrant không hợp lệ.")
        return [point for point in points if isinstance(point, dict)]

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

