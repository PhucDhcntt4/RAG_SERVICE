from __future__ import annotations

import httpx

from app.config import Settings


def get_collection(client: httpx.Client, name: str):
    response = client.get(f"/collections/{name}")

    if response.status_code == 404:
        return None

    response.raise_for_status()
    body = response.json()

    if body.get("status") not in (None, "ok"):
        raise RuntimeError(
            f"Qdrant trả về trạng thái không hợp lệ cho collection {name}"
        )

    return body.get("result") or {}


def create_collection(
    client: httpx.Client,
    *,
    name: str,
    vector_size: int,
    distance: str,
    hnsw_m: int | None = None,
):
    payload = {
        "vectors": {
            "size": vector_size,
            "distance": distance,
        }
    }

    # Knowledge RAG dùng HNSW mặc định của Qdrant.
    # Product Catalog và Product Images dùng cấu hình riêng.
    if hnsw_m is not None:
        payload["hnsw_config"] = {
            "m": hnsw_m,
        }

    response = client.put(
        f"/collections/{name}",
        json=payload,
    )
    response.raise_for_status()

    body = response.json()
    if body.get("status") not in (None, "ok"):
        raise RuntimeError(
            f"Không thể tạo collection {name}: {body}"
        )


def validate_collection(
    info: dict,
    *,
    name: str,
    vector_size: int,
    distance: str,
):
    vectors = (
        ((info.get("config") or {}).get("params") or {}).get("vectors")
    )

    if not isinstance(vectors, dict):
        raise RuntimeError(
            f"Collection {name} tồn tại nhưng không đọc được cấu hình vector."
        )

    actual_size = vectors.get("size")
    actual_distance = str(vectors.get("distance") or "").lower()

    if actual_size != vector_size:
        raise RuntimeError(
            f"Collection {name} sai vector size: "
            f"đang là {actual_size}, cần {vector_size}."
        )

    if actual_distance != distance.lower():
        raise RuntimeError(
            f"Collection {name} sai distance: "
            f"đang là {vectors.get('distance')}, cần {distance}."
        )


def ensure_collection(
    client: httpx.Client,
    *,
    name: str,
    vector_size: int,
    distance: str,
    hnsw_m: int | None = None,
):
    info = get_collection(client, name)

    if info is None:
        print(f"[CREATE] {name}")

        create_collection(
            client,
            name=name,
            vector_size=vector_size,
            distance=distance,
            hnsw_m=hnsw_m,
        )

        info = get_collection(client, name)
        if info is None:
            raise RuntimeError(
                f"Đã tạo nhưng không đọc lại được collection {name}"
            )

        validate_collection(
            info,
            name=name,
            vector_size=vector_size,
            distance=distance,
        )

        hnsw_text = (
            f", hnsw_m={hnsw_m}"
            if hnsw_m is not None
            else ", hnsw=Qdrant default"
        )

        print(
            f"[OK] {name} created "
            f"(size={vector_size}, distance={distance}{hnsw_text})"
        )
        return

    validate_collection(
        info,
        name=name,
        vector_size=vector_size,
        distance=distance,
    )

    points_count = int(info.get("points_count") or 0)

    print(
        f"[EXISTS] {name} "
        f"(size={vector_size}, distance={distance}, points={points_count})"
    )


def main():
    settings = Settings.load()

    base_url = settings.qdrant_url.rstrip("/")
    timeout = settings.qdrant_timeout_seconds

    print("CREATE RAG COLLECTIONS")
    print(f"Qdrant   : {base_url}")
    print(f"Knowledge: {settings.qdrant_collection}")
    print(f"Catalog  : {settings.product_catalog_collection}")
    print(f"Images   : {settings.product_image_collection}")

    try:
        with httpx.Client(
            base_url=base_url,
            timeout=timeout,
        ) as client:

            # 1. Knowledge RAG
            # Gemini text embedding của project dùng vector 768 chiều.
            ensure_collection(
                client,
                name=settings.qdrant_collection,
                vector_size=768,
                distance="Cosine",
            )

            # 2. Product Catalog
            # Chỉ lưu payload sản phẩm; vector [1.0] là dummy vector.
            ensure_collection(
                client,
                name=settings.product_catalog_collection,
                vector_size=1,
                distance="Cosine",
                hnsw_m=0,
            )

            # 3. Product Images
            # OpenCLIP ViT-B-32 tạo vector ảnh 512 chiều.
            ensure_collection(
                client,
                name=settings.product_image_collection,
                vector_size=512,
                distance="Cosine",
                hnsw_m=16,
            )

    except httpx.ConnectError as exc:
        raise SystemExit(
            f"Không kết nối được Qdrant tại {base_url}. "
            "Kiểm tra Qdrant đang chạy và QDRANT_URL trong .env."
        ) from exc

    except httpx.HTTPStatusError as exc:
        raise SystemExit(
            f"Qdrant HTTP error: {exc.response.status_code} "
            f"{exc.response.text[:500]}"
        ) from exc

    except Exception as exc:
        raise SystemExit(f"LỖI: {exc}") from exc

    print("HOÀN TẤT: 3 RAG collections đã sẵn sàng.")


if __name__ == "__main__":
    main()