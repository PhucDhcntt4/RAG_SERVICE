"""Associate extracted PDF images with the structural section above them."""
from dataclasses import dataclass

from app.pdf_extract import PdfExtraction
from app.pdf_structure import detect_headings


@dataclass(frozen=True)
class PreparedPdfAsset:
    page_number: int
    bbox: tuple[float, float, float, float]
    width: int
    height: int
    checksum: str
    section_path: tuple[str, ...]
    content: bytes
    media_type: str = "image/png"
    asset_type: str = "image"


def prepare_pdf_assets(extraction: PdfExtraction) -> list[PreparedPdfAsset]:
    """Attach each image to the latest heading in PDF reading order."""
    if not extraction.images:
        return []

    headings = sorted(
        (
            (item.line.page, item.line.bbox[1], item.level, item.line.text[:500])
            for item in detect_headings(extraction)
            if item.level is not None
        ),
        key=lambda value: (value[0], value[1]),
    )
    assets = []
    stack: list[tuple[int, str]] = []
    heading_index = 0

    for image in sorted(extraction.images, key=lambda item: (item.page, item.bbox[1], item.bbox[0])):
        image_y = image.bbox[1]
        while heading_index < len(headings):
            page, y, level, title = headings[heading_index]
            if (page, y) > (image.page, image_y):
                break
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            heading_index += 1
        assets.append(PreparedPdfAsset(
            page_number=image.page,
            bbox=image.bbox,
            width=image.width,
            height=image.height,
            checksum=image.digest,
            section_path=tuple(title for _, title in stack),
            content=image.png_data,
        ))
    return assets
