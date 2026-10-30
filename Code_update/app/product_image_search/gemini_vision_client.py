"""Gemini Vision client for classifying and locating a product in an image."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.product_image_search.image_processing_service import ProductBoundingBox


ANALYSIS_PROMPT_PATH = (
    Path(__file__).resolve().parent
    / "prompts"
    / "product_vision_analysis.txt"
)


class GeminiVisionError(RuntimeError):
    """Raised when Gemini cannot produce a valid product-image analysis."""


def load_product_vision_analysis_prompt() -> str:
    """Load the editable product-analysis prompt from disk."""

    try:
        prompt = ANALYSIS_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise GeminiVisionError(
            "Không thể đọc prompt phân tích ảnh sản phẩm."
        ) from exc

    if not prompt:
        raise GeminiVisionError(
            "Prompt phân tích ảnh sản phẩm đang để trống."
        )
    return prompt


@dataclass(frozen=True, slots=True)
class ProductVisionAnalysis:
    """Validated classification and normalized bounding box from Gemini."""

    product_detected: bool
    category: str
    label: str
    bounding_box: ProductBoundingBox | None
    confidence: float
    description: str


class GeminiVisionClient:
    """Send one inline image to Gemini and validate its structured output."""

    ALLOWED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp"}
    ALLOWED_CATEGORIES = {
        "shoe",
        "sandal",
        "slipper",
        "bag",
        "wallet",
        "belt",
        "accessory",
        "unknown",
    }
    RESPONSE_SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "product_detected": {
                "type": "boolean",
                "description": "Có phát hiện được sản phẩm thời trang hay không.",
            },
            "category": {
                "type": "string",
                "enum": sorted(ALLOWED_CATEGORIES),
            },
            "label": {
                "type": "string",
                "description": "Nhãn mô tả ngắn bằng tiếng Việt.",
            },
            "box_2d": {
                "type": "array",
                "description": (
                    "Bounding box dạng [ymin, xmin, ymax, xmax], "
                    "tọa độ từ 0 đến 1000."
                ),
                "items": {"type": "integer"},
                "minItems": 4,
                "maxItems": 4,
            },
            "confidence": {
                "type": "number",
                "description": "Độ tự tin từ 0 đến 1.",
            },
            "description": {
                "type": "string",
                "description": "Mô tả ngắn đặc điểm nhìn thấy của sản phẩm.",
            },
        },
        "required": [
            "product_detected",
            "category",
            "label",
            "box_2d",
            "confidence",
            "description",
        ],
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
            raise ValueError("Gemini Vision model không được để trống.")
        if not normalized_base_url.startswith(("http://", "https://")):
            raise ValueError("Gemini base URL không hợp lệ.")
        if timeout <= 0:
            raise ValueError("Gemini Vision timeout phải lớn hơn 0.")

        self.api_key = normalized_key
        self.model = normalized_model
        self.base_url = normalized_base_url
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5.0),
            follow_redirects=False,
        )

    @staticmethod
    def _extract_response_text(body: dict[str, Any]) -> str:
        try:
            parts = body["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GeminiVisionError(
                "Gemini Vision trả response không đúng định dạng."
            ) from exc

        text = "".join(
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, dict)
        ).strip()
        if not text:
            raise GeminiVisionError(
                "Gemini Vision không trả kết quả phân tích."
            )
        return text

    @staticmethod
    def _parse_box(raw_box: Any) -> ProductBoundingBox:
        if not isinstance(raw_box, list) or len(raw_box) != 4:
            raise GeminiVisionError(
                "Gemini Vision trả bounding box không hợp lệ."
            )
        try:
            y_min, x_min, y_max, x_max = [float(value) for value in raw_box]
        except (TypeError, ValueError) as exc:
            raise GeminiVisionError(
                "Tọa độ bounding box không phải dạng số."
            ) from exc

        coordinates = (y_min, x_min, y_max, x_max)
        if any(value < 0 or value > 1000 for value in coordinates):
            raise GeminiVisionError(
                "Tọa độ bounding box nằm ngoài khoảng 0 đến 1000."
            )
        if x_min >= x_max or y_min >= y_max:
            raise GeminiVisionError(
                "Thứ tự tọa độ bounding box không hợp lệ."
            )

        return ProductBoundingBox(
            x_min=x_min / 1000,
            y_min=y_min / 1000,
            x_max=x_max / 1000,
            y_max=y_max / 1000,
        )

    @classmethod
    def _parse_analysis(cls, payload: Any) -> ProductVisionAnalysis:
        if not isinstance(payload, dict):
            raise GeminiVisionError(
                "Dữ liệu phân tích của Gemini không hợp lệ."
            )

        product_detected = payload.get("product_detected")
        if not isinstance(product_detected, bool):
            raise GeminiVisionError(
                "Gemini không trả trạng thái phát hiện hợp lệ."
            )

        category = str(payload.get("category") or "unknown").strip().lower()
        if category not in cls.ALLOWED_CATEGORIES:
            raise GeminiVisionError(
                "Gemini trả nhóm sản phẩm không được hỗ trợ."
            )

        try:
            confidence = float(payload.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise GeminiVisionError(
                "Gemini trả độ tự tin không hợp lệ."
            ) from exc
        if not 0 <= confidence <= 1:
            raise GeminiVisionError(
                "Độ tự tin của Gemini phải nằm trong khoảng 0 đến 1."
            )

        bounding_box = None
        if product_detected:
            bounding_box = cls._parse_box(payload.get("box_2d"))

        return ProductVisionAnalysis(
            product_detected=product_detected,
            category=category,
            label=str(payload.get("label") or "").strip(),
            bounding_box=bounding_box,
            confidence=confidence,
            description=str(payload.get("description") or "").strip(),
        )

    async def analyze(
        self,
        *,
        data: bytes,
        media_type: str,
    ) -> ProductVisionAnalysis:
        if not data:
            raise GeminiVisionError("Ảnh gửi đến Gemini không có dữ liệu.")

        normalized_media_type = media_type.split(";", 1)[0].strip().lower()
        if normalized_media_type not in self.ALLOWED_MEDIA_TYPES:
            raise GeminiVisionError(
                "Gemini Vision chỉ hỗ trợ ảnh JPEG, PNG và WebP."
            )

        encoded_image = base64.b64encode(data).decode("ascii")
        safe_model = quote(self.model, safe="-._")
        request_body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": normalized_media_type,
                                "data": encoded_image,
                            }
                        },
                        {"text": load_product_vision_analysis_prompt()},
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "responseSchema": self.RESPONSE_SCHEMA,
            },
        }

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
            raise GeminiVisionError(
                "Gemini Vision phản hồi quá thời gian."
            ) from exc
        except httpx.RequestError as exc:
            raise GeminiVisionError(
                "Không thể kết nối Gemini Vision."
            ) from exc

        if not response.is_success:
            raise GeminiVisionError(
                f"Gemini Vision trả lỗi HTTP {response.status_code}."
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise GeminiVisionError(
                "Gemini Vision trả dữ liệu JSON không hợp lệ."
            ) from exc

        response_text = self._extract_response_text(body)
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise GeminiVisionError(
                "Nội dung Gemini Vision trả về không phải JSON."
            ) from exc
        return self._parse_analysis(payload)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


