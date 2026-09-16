"""Ruled PDF tables: preserve physical cells instead of guessing from plain text."""
from dataclasses import dataclass
from threading import Lock


_TABLE_LOCK = Lock()


@dataclass(frozen=True)
class PdfTable:
    page: int
    bbox: tuple[float, float, float, float]
    rows: tuple[str, ...]
    row_headers: tuple[str, ...] = ()


def extract_tables(page, page_number: int) -> tuple[PdfTable, ...]:
    # PyMuPDF's table finder uses module-level working buffers.
    with _TABLE_LOCK:
        return _extract_tables(page, page_number)


def _extract_tables(page, page_number: int) -> tuple[PdfTable, ...]:
    tables = []
    spans = [span for block in page.get_text("dict", flags=0)["blocks"]
             for line in block.get("lines", []) for span in line["spans"]]

    def bold_cell(rect):
        contained = [span for span in spans
                     if rect[0] <= (span["bbox"][0] + span["bbox"][2]) / 2 <= rect[2]
                     and rect[1] <= (span["bbox"][1] + span["bbox"][3]) / 2 <= rect[3]]
        total = sum(len(span["text"].strip()) for span in contained)
        bold = sum(len(span["text"].strip()) for span in contained if span["flags"] & 16)
        return total > 0 and bold / total >= .6
    # Ignore text background rectangles, which otherwise create false cells.
    for table in page.find_tables(strategy="lines_strict").tables:
        if table.col_count < 2:
            continue
        matrix = table.extract()
        cells = {}
        for row, values in zip(table.rows, matrix):
            for rect, value in zip(row.cells, values):
                if rect is not None:
                    cells[tuple(rect)] = " ".join((value or "").split())
        if not any(cells.values()):
            continue
        xs = sorted({round(rect[i], 2) for rect in cells for i in (0, 2)})
        ys = sorted({round(rect[i], 2) for rect in cells for i in (1, 3)})
        rows, row_headers = [], []
        header = ""
        for y0, y1 in zip(ys, ys[1:]):
            middle = (y0 + y1) / 2
            parts = []
            occupied = []
            # A vertically merged cell applies only within its actual rectangle.
            # Empty cells stay empty; never fill them from the previous row.
            for rect, value in sorted(cells.items()):
                if not value or not rect[1] - .02 <= middle < rect[3] + .02:
                    continue
                first = min(range(len(xs)), key=lambda i: abs(xs[i] - rect[0])) + 1
                last = min(range(len(xs)), key=lambda i: abs(xs[i] - rect[2]))
                column = str(first) if first == last else f"{first}–{last}"
                parts.append(f"Cột {column}: {value}")
                occupied.append((rect, value))
            if parts:
                if (len(occupied) >= 2 and all(
                    rect[1] >= y0 - .1 and rect[3] <= y1 + .1 and bold_cell(rect)
                    for rect, _ in occupied
                )):
                    header = " | ".join(value for _, value in occupied)
                rows.append(f"Dòng {len(rows) + 1}: " + " | ".join(parts))
                row_headers.append(header)
        tables.append(PdfTable(page_number, tuple(table.bbox), tuple(rows), tuple(row_headers)))
    return tuple(tables)


def inside_table(line, table: PdfTable) -> bool:
    x = (line.bbox[0] + line.bbox[2]) / 2
    y = (line.bbox[1] + line.bbox[3]) / 2
    x0, y0, x1, y1 = table.bbox
    return line.page == table.page and x0 <= x <= x1 and y0 <= y <= y1
