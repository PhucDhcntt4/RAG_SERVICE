from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values

from app.config import ROOT


def _safe_code(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return value or "unknown"


def _extension(url: str, content_type: str) -> str:
    content_type = (content_type or "").split(";", 1)[0].strip().lower()

    by_type = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
    }
    if content_type in by_type:
        return by_type[content_type]

    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}:
        return ".jpg" if suffix == ".jpeg" else suffix

    guessed = mimetypes.guess_extension(content_type) if content_type else None
    return guessed or ".jpg"


@dataclass
class DownloadedImage:
    data: bytes
    checksum: str
    relative_path: str
    mime_type: str


class ProductImageStore:
    def __init__(self):
        values = {**dotenv_values(ROOT / ".env"), **os.environ}
        configured = str(
            values.get("PRODUCT_IMAGE_STORAGE_DIR") or "storage/product_images"
        ).strip()

        root = Path(configured)
        if not root.is_absolute():
            root = ROOT / root

        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

        raw_limit = str(values.get("PRODUCT_IMAGE_MAX_BYTES") or "20971520")
        try:
            self.max_bytes = max(1024 * 1024, int(raw_limit))
        except ValueError:
            self.max_bytes = 20 * 1024 * 1024

        self.client = httpx.Client(
            timeout=httpx.Timeout(45.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": "DongHai-RAG-ProductSync/2C"},
        )

    def close(self):
        self.client.close()

    def existing_bytes(self, relative_path: str) -> bytes | None:
        relative_path = str(relative_path or "").strip()
        if not relative_path:
            return None

        path = (self.root / relative_path).resolve()
        root = self.root.resolve()

        try:
            path.relative_to(root)
        except ValueError:
            return None

        if not path.is_file():
            return None

        return path.read_bytes()

    def download(self, url: str, product_code: str) -> DownloadedImage:
        last_error = None

        for attempt in range(4):
            try:
                with self.client.stream("GET", url) as response:
                    response.raise_for_status()

                    chunks = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise RuntimeError(
                                f"Ảnh vượt PRODUCT_IMAGE_MAX_BYTES: {url}"
                            )
                        chunks.append(chunk)

                    data = b"".join(chunks)
                    if not data:
                        raise RuntimeError(f"Ảnh rỗng: {url}")

                    checksum = hashlib.sha256(data).hexdigest()
                    mime_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                    ) or "image/jpeg"

                    ext = _extension(url, mime_type)
                    relative = (
                        Path(_safe_code(product_code))
                        / f"{checksum}{ext}"
                    )
                    absolute = self.root / relative
                    absolute.parent.mkdir(parents=True, exist_ok=True)

                    if not absolute.exists():
                        tmp = absolute.with_suffix(absolute.suffix + ".tmp")
                        tmp.write_bytes(data)
                        tmp.replace(absolute)

                    return DownloadedImage(
                        data=data,
                        checksum=checksum,
                        relative_path=relative.as_posix(),
                        mime_type=mime_type,
                    )

            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                break

        raise RuntimeError(f"Không tải được ảnh {url}") from last_error
