"""Chuẩn hóa và crop ảnh cho pipeline nhận diện sản phẩm."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import math

from PIL import Image, ImageOps, UnidentifiedImageError


class ImageProcessingError(RuntimeError):
    """Lỗi xảy ra khi không thể xử lý ảnh."""


class InvalidBoundingBoxError(ImageProcessingError):
    """Bounding box không hợp lệ hoặc quá nhỏ."""


@dataclass(frozen=True, slots=True)
class ProductBoundingBox:
    """Bounding box chuẩn hóa trong khoảng 0.0 đến 1.0."""

    x_min: float
    y_min: float
    x_max: float
    y_max: float


@dataclass(frozen=True, slots=True)
class ProcessedProductImage:
    """Kết quả sau khi chuẩn hóa và crop ảnh."""

    data: bytes
    width: int
    height: int
    original_width: int
    original_height: int
    crop_applied: bool


class ImageProcessingService:
    """Chuẩn hóa hướng ảnh, crop sản phẩm và xuất JPEG cho OpenCLIP."""

    def __init__(
        self,
        *,
        crop_margin_ratio: float = 0.06,
        max_dimension: int = 1600,
        jpeg_quality: int = 90,
        max_pixels: int = 40_000_000,
    ):
        if not 0 <= crop_margin_ratio <= 0.25:
            raise ValueError(
                "crop_margin_ratio phải nằm trong khoảng 0 đến 0.25."
            )
        if max_dimension <= 0:
            raise ValueError("max_dimension phải lớn hơn 0.")
        if not 60 <= jpeg_quality <= 100:
            raise ValueError("jpeg_quality phải nằm trong khoảng 60 đến 100.")
        if max_pixels <= 0:
            raise ValueError("max_pixels phải lớn hơn 0.")

        self.crop_margin_ratio = crop_margin_ratio
        self.max_dimension = max_dimension
        self.jpeg_quality = jpeg_quality
        self.max_pixels = max_pixels

    def _normalize_box(
        self,
        box: ProductBoundingBox,
    ) -> ProductBoundingBox:
        values = (
            box.x_min,
            box.y_min,
            box.x_max,
            box.y_max,
        )

        if not all(math.isfinite(value) for value in values):
            raise InvalidBoundingBoxError(
                "Bounding box chứa tọa độ không hợp lệ."
            )

        x_min = min(1.0, max(0.0, float(box.x_min)))
        y_min = min(1.0, max(0.0, float(box.y_min)))
        x_max = min(1.0, max(0.0, float(box.x_max)))
        y_max = min(1.0, max(0.0, float(box.y_max)))

        if x_min >= x_max or y_min >= y_max:
            raise InvalidBoundingBoxError(
                "Thứ tự tọa độ bounding box không hợp lệ."
            )

        box_width = x_max - x_min
        box_height = y_max - y_min

        if box_width < 0.03 or box_height < 0.03:
            raise InvalidBoundingBoxError(
                "Bounding box quá nhỏ để nhận diện sản phẩm."
            )

        margin_x = box_width * self.crop_margin_ratio
        margin_y = box_height * self.crop_margin_ratio

        return ProductBoundingBox(
            x_min=max(0.0, x_min - margin_x),
            y_min=max(0.0, y_min - margin_y),
            x_max=min(1.0, x_max + margin_x),
            y_max=min(1.0, y_max + margin_y),
        )

    @staticmethod
    def _convert_to_rgb(image: Image.Image) -> Image.Image:
        """Chuyển ảnh sang RGB và thay nền trong suốt bằng màu trắng."""

        has_transparency = (
            image.mode in {"RGBA", "LA"}
            or "transparency" in image.info
        )

        if not has_transparency:
            return image.convert("RGB")

        rgba = image.convert("RGBA")
        background = Image.new(
            "RGB",
            rgba.size,
            color=(255, 255, 255),
        )
        background.paste(
            rgba,
            mask=rgba.getchannel("A"),
        )
        return background

    def process(
        self,
        data: bytes,
        bounding_box: ProductBoundingBox | None = None,
    ) -> ProcessedProductImage:
        if not data:
            raise ImageProcessingError("Ảnh không có dữ liệu.")

        try:
            with Image.open(BytesIO(data)) as opened:
                normalized = ImageOps.exif_transpose(opened)
                normalized.load()
                image = normalized.copy()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ImageProcessingError(
                "File tải lên không phải ảnh hợp lệ hoặc đã bị hỏng."
            ) from exc

        original_width, original_height = image.size

        if original_width <= 0 or original_height <= 0:
            raise ImageProcessingError("Kích thước ảnh không hợp lệ.")

        if original_width * original_height > self.max_pixels:
            raise ImageProcessingError(
                "Kích thước ảnh vượt quá giới hạn cho phép."
            )

        crop_applied = False

        if bounding_box is not None:
            box = self._normalize_box(bounding_box)

            left = round(box.x_min * image.width)
            top = round(box.y_min * image.height)
            right = round(box.x_max * image.width)
            bottom = round(box.y_max * image.height)

            left = max(0, min(left, image.width - 1))
            top = max(0, min(top, image.height - 1))
            right = max(left + 1, min(right, image.width))
            bottom = max(top + 1, min(bottom, image.height))

            if right <= left or bottom <= top:
                raise InvalidBoundingBoxError(
                    "Không thể chuyển bounding box sang vùng ảnh hợp lệ."
                )

            image = image.crop(
                (
                    left,
                    top,
                    right,
                    bottom,
                )
            )
            crop_applied = True

        if max(image.size) > self.max_dimension:
            image.thumbnail(
                (
                    self.max_dimension,
                    self.max_dimension,
                ),
                Image.Resampling.LANCZOS,
            )

        image = self._convert_to_rgb(image)

        output = BytesIO()

        try:
            image.save(
                output,
                format="JPEG",
                quality=self.jpeg_quality,
                optimize=True,
            )
        except OSError as exc:
            raise ImageProcessingError(
                "Không thể xuất ảnh sau khi xử lý."
            ) from exc

        return ProcessedProductImage(
            data=output.getvalue(),
            width=image.width,
            height=image.height,
            original_width=original_width,
            original_height=original_height,
            crop_applied=crop_applied,
        )
