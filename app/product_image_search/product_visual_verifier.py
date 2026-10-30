"""Gemini client for comparing a query image with product candidates."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


VERIFICATION_PROMPT_PATH = (
    Path(__file__).resolve().parent
    / "prompts"
    / "product_visual_verification.txt"
)


class ProductVisualVerificationError(RuntimeError):
    """Raised when Gemini cannot verify the candidate images."""


def load_product_visual_verification_prompt() -> str:
    try:
        prompt = VERIFICATION_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ProductVisualVerificationError(
            "Không thể đọc prompt kiểm chứng hình ảnh sản phẩm."
        ) from exc
    if not prompt:
        raise ProductVisualVerificationError(
            "Prompt kiểm chứng hình ảnh sản phẩm đang trống."
        )
    return prompt


@dataclass(frozen=True, slots=True)
class ProductVisualCandidate:
    product_code: str
    data: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class ProductVisualMatch:
    product_code: str
    visual_score: float
    reason: str


class ProductVisualVerifier:
    ALLOWED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp"}
    RESPONSE_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "product_code": {"type": "string"},
                        "visual_score": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["product_code", "visual_score", "reason"],
                },
            }
        },
        "required": ["matches"],
    }

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout: float = 45,
        client: httpx.AsyncClient | None = None,
    ):
        normalized_key = api_key.strip()
        normalized_model = model.strip()
        normalized_base_url = base_url.strip().rstrip("/")
        if not normalized_key:
            raise ValueError("Gemini API key không được để trống.")
        if not normalized_model:
            raise ValueError("Gemini model không được để trống.")
        if not normalized_base_url.startswith(("http://", "https://")):
            raise ValueError("Gemini base URL không hợp lệ.")

        self.api_key = normalized_key
        self.model = normalized_model
        self.base_url = normalized_base_url
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5.0),
            follow_redirects=False,
        )

    @classmethod
    def _normalize_media_type(cls, value: str) -> str:
        media_type = str(value or "").split(";", 1)[0].strip().lower()
        if media_type not in cls.ALLOWED_MEDIA_TYPES:
            raise ProductVisualVerificationError(
                "Kiểm chứng chỉ hỗ trợ ảnh JPEG, PNG và WebP."
            )
        return media_type

    @staticmethod
    def _inline_image(data: bytes, media_type: str) -> dict[str, Any]:
        if not data:
            raise ProductVisualVerificationError(
                "Ảnh kiểm chứng không có dữ liệu."
            )
        return {
            "inlineData": {
                "mimeType": media_type,
                "data": base64.b64encode(data).decode("ascii"),
            }
        }

    @staticmethod
    def _extract_text(body: dict[str, Any]) -> str:
        try:
            parts = body["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProductVisualVerificationError(
                "Gemini trả response kiểm chứng không hợp lệ."
            ) from exc
        text = "".join(
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, dict)
        ).strip()
        if not text:
            raise ProductVisualVerificationError(
                "Gemini không trả kết quả kiểm chứng."
            )
        return text

    @staticmethod
    def _parse_matches(
        payload: Any,
        allowed_codes: dict[str, str],
    ) -> list[ProductVisualMatch]:
        if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
            raise ProductVisualVerificationError(
                "Dữ liệu kiểm chứng của Gemini không hợp lệ."
            )

        best_by_code: dict[str, ProductVisualMatch] = {}
        for raw in payload["matches"]:
            if not isinstance(raw, dict):
                continue
            normalized_code = str(raw.get("product_code") or "").strip().upper()
            product_code = allowed_codes.get(normalized_code)
            if product_code is None:
                continue
            try:
                score = float(raw.get("visual_score"))
            except (TypeError, ValueError):
                continue
            if not 0 <= score <= 1:
                continue
            match = ProductVisualMatch(
                product_code=product_code,
                visual_score=score,
                reason=str(raw.get("reason") or "").strip(),
            )
            current = best_by_code.get(product_code)
            if current is None or match.visual_score > current.visual_score:
                best_by_code[product_code] = match

        return sorted(
            best_by_code.values(),
            key=lambda item: item.visual_score,
            reverse=True,
        )

    async def verify(
        self,
        *,
        query_data: bytes,
        query_media_type: str,
        candidates: list[ProductVisualCandidate],
    ) -> list[ProductVisualMatch]:
        if not candidates:
            return []

        query_type = self._normalize_media_type(query_media_type)
        allowed_codes: dict[str, str] = {}
        parts: list[dict[str, Any]] = [
            {"text": load_product_visual_verification_prompt()},
            {"text": "Ảnh khách hàng cần nhận diện:"},
            self._inline_image(query_data, query_type),
        ]
        for index, candidate in enumerate(candidates, start=1):
            product_code = candidate.product_code.strip()
            if not product_code:
                continue
            normalized_code = product_code.upper()
            if normalized_code in allowed_codes:
                continue
            allowed_codes[normalized_code] = product_code
            media_type = self._normalize_media_type(candidate.media_type)
            parts.extend(
                [
                    {
                        "text": (
                            f"Ứng viên {index}; "
                            f"product_code={product_code}:"
                        )
                    },
                    self._inline_image(candidate.data, media_type),
                ]
            )

        if not allowed_codes:
            return []

        request_body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
                "responseSchema": self.RESPONSE_SCHEMA,
            },
        }
        safe_model = quote(self.model, safe="-._")
        try:
            response = await self._client.post(
                f"{self.base_url}/models/{safe_model}:generateContent",
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self.api_key,
                },
                json=request_body,
            )
        except httpx.TimeoutException as exc:
            raise ProductVisualVerificationError(
                "Gemini kiểm chứng hình ảnh quá thời gian."
            ) from exc
        except httpx.RequestError as exc:
            raise ProductVisualVerificationError(
                "Không thể kết nối Gemini để kiểm chứng hình ảnh."
            ) from exc

        if not response.is_success:
            raise ProductVisualVerificationError(
                f"Gemini kiểm chứng trả lỗi HTTP {response.status_code}."
            )
        try:
            body = response.json()
            payload = json.loads(self._extract_text(body))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProductVisualVerificationError(
                "Gemini trả JSON kiểm chứng không hợp lệ."
            ) from exc
        return self._parse_matches(payload, allowed_codes)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

