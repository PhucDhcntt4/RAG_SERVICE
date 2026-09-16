from dataclasses import dataclass

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
class PdfExtraction:
    lines: tuple[PdfLine, ...]
    page_count: int
    pages_without_text: tuple[int, ...]
    tables: tuple[PdfTable, ...] = ()


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

    if not lines:
        raise ValueError(
            "Không đọc được lớp chữ trong PDF. "
            "Nếu là PDF scan, cần OCR."
        )

    return PdfExtraction(
        lines=tuple(lines),
        page_count=page_count,
        pages_without_text=tuple(pages_without_text),
        tables=tuple(tables),
    )
