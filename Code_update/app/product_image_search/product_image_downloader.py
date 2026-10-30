"""Safely download trusted catalog images for visual verification."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx


class ProductImageDownloadError(RuntimeError):
    """Raised when a catalog image cannot be downloaded safely."""


@dataclass(frozen=True, slots=True)
class DownloadedProductImage:
    data: bytes
    media_type: str


def _resolve_host(hostname: str) -> set[str]:
    try:
        records = socket.getaddrinfo(
            hostname,
            None,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ProductImageDownloadError(
            "Không thể phân giải tên miền ảnh sản phẩm."
        ) from exc
    return {str(record[4][0]).split("%", 1)[0] for record in records}


class ProductImageDownloader:
    ALLOWED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp"}

    def __init__(
        self,
        *,
        max_bytes: int,
        timeout: float,
        client: httpx.AsyncClient | None = None,
        resolver: Callable[[str], Iterable[str]] | None = None,
    ):
        if max_bytes <= 0:
            raise ValueError("Giới hạn dung lượng ảnh phải lớn hơn 0.")
        if timeout <= 0:
            raise ValueError("Timeout tải ảnh phải lớn hơn 0.")
        self.max_bytes = max_bytes
        self._resolver = resolver or _resolve_host
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            follow_redirects=False,
            headers={
                "Accept": "image/jpeg,image/png,image/webp",
                "User-Agent": "RAG-Client-Product-Image-Verifier/1.0",
            },
        )

    async def _validate_url(self, url: str) -> str:
        normalized = str(url or "").strip()
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ProductImageDownloadError("URL ảnh sản phẩm không hợp lệ.")
        if parsed.username or parsed.password:
            raise ProductImageDownloadError(
                "URL ảnh sản phẩm không được chứa thông tin đăng nhập."
            )

        hostname = parsed.hostname.rstrip(".").lower()
        if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
            raise ProductImageDownloadError("Không cho phép tải ảnh từ mạng nội bộ.")

        try:
            literal_ip = ipaddress.ip_address(hostname)
            addresses = {str(literal_ip)}
        except ValueError:
            addresses = set(
                await asyncio.to_thread(self._resolver, hostname)
            )

        if not addresses:
            raise ProductImageDownloadError(
                "Tên miền ảnh sản phẩm không có địa chỉ IP."
            )
        try:
            unsafe = [
                address
                for address in addresses
                if not ipaddress.ip_address(address).is_global
            ]
        except ValueError as exc:
            raise ProductImageDownloadError(
                "Tên miền ảnh sản phẩm trả địa chỉ IP không hợp lệ."
            ) from exc
        if unsafe:
            raise ProductImageDownloadError(
                "Không cho phép tải ảnh từ mạng nội bộ."
            )
        return normalized

    async def download(self, url: str) -> DownloadedProductImage:
        safe_url = await self._validate_url(url)
        try:
            async with self._client.stream("GET", safe_url) as response:
                response.raise_for_status()
                media_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if media_type not in self.ALLOWED_MEDIA_TYPES:
                    raise ProductImageDownloadError(
                        "Tài nguyên tham chiếu không phải ảnh JPEG, PNG hoặc WebP."
                    )

                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError:
                        declared_size = 0
                    if declared_size > self.max_bytes:
                        raise ProductImageDownloadError(
                            "Ảnh tham chiếu vượt quá dung lượng cho phép."
                        )

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise ProductImageDownloadError(
                            "Ảnh tham chiếu vượt quá dung lượng cho phép."
                        )
                    chunks.append(chunk)
        except ProductImageDownloadError:
            raise
        except httpx.TimeoutException as exc:
            raise ProductImageDownloadError(
                "Tải ảnh tham chiếu quá thời gian."
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ProductImageDownloadError(
                f"Tải ảnh tham chiếu thất bại (HTTP {exc.response.status_code})."
            ) from exc
        except httpx.RequestError as exc:
            raise ProductImageDownloadError(
                "Không thể tải ảnh tham chiếu."
            ) from exc

        data = b"".join(chunks)
        if not data:
            raise ProductImageDownloadError("Ảnh tham chiếu không có dữ liệu.")
        return DownloadedProductImage(data=data, media_type=media_type)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


