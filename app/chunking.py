from dataclasses import dataclass

from app.text_structure import detect_text_headings


@dataclass(frozen=True)
class Chunk:
    index: int
    heading: str | None
    content: str
    section_path: tuple[str, ...] = ()


def chunk_text(
    text: str, size: int = 1200, overlap: int = 180, *, natural_headings: bool = True,
) -> list[Chunk]:
    """Split text within sections and preserve ancestor headings."""
    if size < 200 or not 0 <= overlap < size:
        raise ValueError("Invalid chunk size or overlap")

    sections = []
    stack = []
    lines = []

    for item in detect_text_headings(text, natural=natural_headings):
        if item.level is not None:
            # Lưu nội dung theo đường dẫn hiện tại trước khi đổi heading.
            path = tuple(title for _, title in stack)
            sections.append((path, "\n".join(lines).strip()))

            level = item.level
            title = item.text[:500]

            # Bỏ mục cùng cấp hoặc sâu hơn.
            while stack and stack[-1][0] >= level:
                stack.pop()

            stack.append((level, title))
            lines = []
        else:
            lines.append(item.text)

    # Lưu phần nội dung cuối tài liệu.
    path = tuple(title for _, title in stack)
    sections.append((path, "\n".join(lines).strip()))

    return chunk_sections(sections, size, overlap)


def chunk_sections(
    sections: list[tuple[tuple[str, ...], str]],
    size: int = 1200,
    overlap: int = 180,
) -> list[Chunk]:
    """Split section bodies into character windows without crossing headings."""
    if size < 200 or not 0 <= overlap < size:
        raise ValueError("Invalid chunk size or overlap")

    chunks = []

    for path, content in sections:
        heading = path[-1] if path else None
        start = 0

        while start < len(content):
            end = min(start + size, len(content))

            if end < len(content):
                boundary = max(
                    content.rfind("\n", start, end),
                    content.rfind(" ", start, end),
                )

                if boundary > start + max(overlap, size // 2):
                    end = boundary

            value = content[start:end].strip()

            if value:
                chunks.append(Chunk(
                    index=len(chunks),
                    heading=heading,
                    content=value,
                    section_path=path,
                ))

            if end == len(content):
                break

            start = max(start + 1, end - overlap)

    return chunks
