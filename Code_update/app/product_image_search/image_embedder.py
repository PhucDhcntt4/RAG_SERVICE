"""Lazy OpenCLIP image embedding used by product visual search."""

from __future__ import annotations

import io
import threading
from typing import Any


class ImageEmbeddingError(RuntimeError):
    """Raised when an uploaded image cannot be validated or embedded."""


class InvalidProductImageError(ImageEmbeddingError):
    """Raised when uploaded bytes are not a valid supported image."""


class ProductImageEmbedder:
    DIMENSION = 512

    def __init__(self, *, model_name: str, pretrained: str, device: str):
        self.model_name = model_name
        self.pretrained = pretrained
        self.requested_device = device
        self.device = device
        self._model: Any | None = None
        self._preprocess: Any | None = None
        self._torch: Any | None = None
        self._image: Any | None = None
        self._image_ops: Any | None = None
        self._load_lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return

        with self._load_lock:
            if self._model is not None:
                return

            try:
                import open_clip  # type: ignore
                import torch  # type: ignore
                from PIL import Image, ImageOps  # type: ignore
            except ImportError as exc:
                raise ImageEmbeddingError(
                    "Thiếu thư viện tìm kiếm ảnh. Hãy cài lại requirements.txt."
                ) from exc

            device = self.requested_device
            if device == "auto":
                device = "cuda" if torch.cuda.is_available() else "cpu"

            try:
                model, _, preprocess = open_clip.create_model_and_transforms(
                    self.model_name,
                    pretrained=self.pretrained,
                    device=device,
                )
                model.eval()
            except Exception as exc:
                raise ImageEmbeddingError(
                    "Không thể nạp model OpenCLIP. Kiểm tra model, cache và kết nối mạng."
                ) from exc

            self.device = device
            self._torch = torch
            self._image = Image
            self._image_ops = ImageOps
            self._model = model
            self._preprocess = preprocess

    def embed(self, data: bytes) -> list[float]:
        self._ensure_loaded()
        assert self._image is not None
        assert self._image_ops is not None
        assert self._torch is not None
        assert self._model is not None
        assert self._preprocess is not None

        try:
            with self._image.open(io.BytesIO(data)) as opened:
                opened.verify()
            with self._image.open(io.BytesIO(data)) as opened:
                image = self._image_ops.exif_transpose(opened).convert("RGB")
                tensor = self._preprocess(image).unsqueeze(0).to(self.device)
        except Exception as exc:
            raise InvalidProductImageError(
                "File tải lên không phải ảnh hợp lệ hoặc đã bị hỏng."
            ) from exc

        try:
            with self._torch.no_grad():
                vector = self._model.encode_image(tensor)
                vector = vector / vector.norm(dim=-1, keepdim=True)
            result = vector[0].detach().float().cpu().tolist()
        except Exception as exc:
            raise ImageEmbeddingError("Không thể phân tích ảnh sản phẩm.") from exc

        if len(result) != self.DIMENSION:
            raise ImageEmbeddingError(
                f"OpenCLIP trả vector {len(result)} chiều, cần {self.DIMENSION} chiều."
            )
        return result


