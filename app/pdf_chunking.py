from dataclasses import replace

from app.chunking import Chunk, chunk_sections
from app.pdf_extract import PdfExtraction, PdfLine
from app.pdf_structure import DetectedLine, detect_headings
from app.pdf_tables import inside_table


def prepare_pdf_document(
    extraction: PdfExtraction,
    size: int = 1200,
    overlap: int = 180,
    *,
    split_sections: bool = True,
) -> tuple[str, list[Chunk]]:
    """Keep heading paths and complete table rows, including merged cell scopes."""
    if size < 200 or not 0 <= overlap < size:
        raise ValueError("Invalid chunk size or overlap")
    lines = list(extraction.lines)
    markers = {}
    if extraction.tables:
        lines = [line for line in lines
                 if not any(inside_table(line, table) for table in extraction.tables)]
        for index, table in enumerate(extraction.tables):
            marker = PdfLine("", table.page, -index - 1, table.bbox, 0, 0, ())
            lines.append(marker)
            markers[(table.page, marker.block_id)] = table
        lines.sort(key=lambda line: (line.page, line.bbox[1], line.bbox[0]))
    detected = detect_headings(replace(extraction, lines=tuple(lines)))
    if not detected:
        detected = [DetectedLine(line, None, "body") for line in lines]
    chunks, stack, body_lines, source = [], [], [], []
    previous_table = None
    table_heading = ""
    text_since_table = False

    def append_chunk(content):
        path = tuple(title for _, title in stack)
        chunks.append(Chunk(len(chunks), path[-1] if path else None, content, path))

    def flush_section():
        content = "\n".join(body_lines).strip()
        path = tuple(title for _, title in stack)
        if split_sections:
            for chunk in chunk_sections([(path, content)], size, overlap):
                chunks.append(replace(chunk, index=len(chunks)))
        elif content:
            chunks.append(Chunk(
                index=len(chunks),
                heading=path[-1] if path else None,
                content=content,
                section_path=path,
            ))
        body_lines.clear()

    for item in detected:
        table = markers.get((item.line.page, item.line.block_id))
        if table is not None:
            flush_section()
            # Carry a standalone header across a page break only when bounds
            # align and no intervening body/heading establishes a new scope.
            continuation = (
                previous_table is not None and not text_since_table
                and table.page == previous_table.page + 1
                and abs(table.bbox[0] - previous_table.bbox[0]) < 4
                and abs(table.bbox[2] - previous_table.bbox[2]) < 4
            )
            if not continuation:
                table_heading = ""
            base_prefix = f"Bảng, trang PDF {table.page}. Các ô cùng dòng thuộc cùng một bản ghi."
            prefix = base_prefix
            pending = []
            for index, row in enumerate(table.rows):
                heading = (table.row_headers[index] if table.row_headers else "") or table_heading
                new_prefix = base_prefix + ("\nTiêu đề nhóm cột: " + heading if heading else "")
                if pending and new_prefix != prefix:
                    append_chunk(prefix + "\n" + "\n".join(pending))
                    pending = []
                prefix, table_heading = new_prefix, heading
                if pending and len(prefix) + 1 + len("\n".join([*pending, row])) > size:
                    append_chunk(prefix + "\n" + "\n".join(pending))
                    pending = []
                # A single long row stays intact; overall document limits apply.
                pending.append(row)
                source.append(prefix + "\n" + row)
            if pending:
                append_chunk(prefix + "\n" + "\n".join(pending))
            previous_table, text_since_table = table, False
            continue
        source.append(item.line.text)
        # Printed page numbers may differ from the physical PDF page index.
        footer_number = (item.line.text.strip().isdigit()
                         and item.line.bbox[1] >= item.line.page_height * .9)
        if item.line.text.strip() and not footer_number:
            text_since_table = True
        if item.level is None:
            if not footer_number:
                body_lines.append(item.line.text)
            continue

        flush_section()
        while stack and stack[-1][0] >= item.level:
            stack.pop()
        stack.append((item.level, item.line.text))

    flush_section()
    return "\n".join(source), chunks
