from dataclasses import dataclass
import hashlib

import pymupdf

from app.pdf_tables import PdfTable, extract_tables


@dataclass(frozen=True)
class PdfSpan:
    text: str
    font: str
    font_size: float
    bold: bool
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class PdfLine:
    text: str
    page: int
    block_id: int
    bbox: tuple[float, float, float, float]
    page_width: float
    page_height: float
    spans: tuple[PdfSpan, ...]


@dataclass(frozen=True)
class PdfImage:
    page: int
    bbox: tuple[float, float, float, float]
    width: int
    height: int
    digest: str
    png_data: bytes


@dataclass(frozen=True)
class PdfExtraction:
    lines: tuple[PdfLine, ...]
    page_count: int
    pages_without_text: tuple[int, ...]
    tables: tuple[PdfTable, ...] = ()
    images: tuple[PdfImage, ...] = ()


def extract_pdf(
    data: bytes,
    *,
    max_pages: int = 200,
    max_chars: int = 200_000,
    max_bytes: int = 20 * 1024 * 1024,
) -> PdfExtraction:
    if not data:
        raise ValueError("File PDF rỗng.")

    if len(data) > max_bytes:
        raise ValueError("PDF vượt giới hạn dung lượng.")

    lines = []
    pages_without_text = []
    total_chars = 0
    tables = []
    image_candidates = []
    total_image_bytes = 0

    # Đọc chữ và định dạng, không lấy dữ liệu ảnh nhúng.
    flags = (
        pymupdf.TEXTFLAGS_DICT
        & ~pymupdf.TEXT_PRESERVE_IMAGES
    )

    with pymupdf.open(stream=data, filetype="pdf") as document:
        if document.is_encrypted or document.needs_pass:
            raise ValueError("PDF mã hóa hoặc yêu cầu mật khẩu không được hỗ trợ.")

        page_count = len(document)

        if page_count > max_pages:
            raise ValueError("PDF vượt giới hạn số trang.")

        for page_index, page in enumerate(document):
            page_number = page_index + 1
            count_before = len(lines)

            layout = page.get_text(
                "dict",
                flags=flags,
                sort=True,
            )

            for block_index, block in enumerate(layout["blocks"]):
                if block.get("type") != 0:
                    continue

                for raw_line in block.get("lines", []):
                    spans = []

                    for raw_span in raw_line.get("spans", []):
                        span_text = raw_span.get("text", "")

                        if not span_text:
                            continue

                        spans.append(PdfSpan(
                            text=span_text,
                            font=raw_span.get("font", ""),
                            font_size=float(raw_span["size"]),
                            bold=bool(
                                raw_span.get("flags", 0)
                                & pymupdf.TEXT_FONT_BOLD
                            ),
                            bbox=tuple(raw_span["bbox"]),
                        ))

                    line_text = "".join(
                        span.text for span in spans
                    ).strip()

                    if not line_text:
                        continue

                    total_chars += len(line_text) + 1

                    if total_chars > max_chars:
                        raise ValueError(
                            "PDF vượt giới hạn nội dung văn bản."
                        )

                    lines.append(PdfLine(
                        text=line_text,
                        page=page_number,
                        block_id=block.get("number", block_index),
                        bbox=tuple(raw_line["bbox"]),
                        page_width=float(layout["width"]),
                        page_height=float(layout["height"]),
                        spans=tuple(spans),
                    ))

            if len(lines) == count_before:
                pages_without_text.append(page_number)
            else:
                tables.extend(extract_tables(page, page_number))

            # Render each placed raster image from its page rectangle. Rendering
            # the clip also handles masks and transparency consistently.
            seen_rectangles = set()
            for image_info in page.get_image_info(hashes=True, xrefs=True):
                if len(image_candidates) >= 200 or total_image_bytes >= 40 * 1024 * 1024:
                    break
                bbox = tuple(float(value) for value in image_info.get("bbox", ()))
                if len(bbox) != 4:
                    continue
                rect_key = tuple(round(value, 2) for value in bbox)
                if rect_key in seen_rectangles:
                    continue
                seen_rectangles.add(rect_key)
                width = int(image_info.get("width") or 0)
                height = int(image_info.get("height") or 0)
                area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
                page_area = max(1.0, float(page.rect.width * page.rect.height))
                # Ignore tiny icons, bullets and tracking pixels.
                if width < 64 or height < 64 or area / page_area < 0.005:
                    continue
                digest_value = image_info.get("digest")
                digest = (
                    bytes(digest_value).hex()
                    if isinstance(digest_value, (bytes, bytearray))
                    else hashlib.sha256(
                        repr((page_number, rect_key, width, height)).encode()
                    ).hexdigest()
                )
                pixmap = page.get_pixmap(
                    matrix=pymupdf.Matrix(1.5, 1.5),
                    clip=pymupdf.Rect(bbox),
                    alpha=False,
                    colorspace=pymupdf.csRGB,
                )
                png_data = pixmap.tobytes("png")
                if total_image_bytes + len(png_data) > 40 * 1024 * 1024:
                    continue
                image_candidates.append(PdfImage(
                    page=page_number,
                    bbox=bbox,
                    width=pixmap.width,
                    height=pixmap.height,
                    digest=digest,
                    png_data=png_data,
                ))
                total_image_bytes += len(png_data)

    if not lines:
        raise ValueError(
            "Không đọc được lớp chữ trong PDF. "
            "Nếu là PDF scan, cần OCR."
        )

    # The same image on at least three pages is normally a logo/header/footer.
    digest_pages = {}
    for image in image_candidates:
        digest_pages.setdefault(image.digest, set()).add(image.page)
    repeated_decorations = {
        digest for digest, pages in digest_pages.items() if len(pages) >= 3
    }
    images = [
        image for image in image_candidates
        if image.digest not in repeated_decorations
    ][:100]

    return PdfExtraction(
        lines=tuple(lines),
        page_count=page_count,
        pages_without_text=tuple(pages_without_text),
        tables=tuple(tables),
        images=tuple(images),
    )
