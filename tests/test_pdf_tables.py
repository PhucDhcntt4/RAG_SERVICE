import unittest
import pymupdf

from app.pdf_extract import PdfExtraction, extract_pdf
from app.pdf_chunking import prepare_pdf_document
from app.pdf_tables import PdfTable
from tests.test_pdf_structure import line


def table_pdf():
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 40), "4. Policy", fontname="hebo", fontsize=15)
        # Two header groups; a vertical merged group, a blank cell, and
        # a horizontally merged penalty on the last row.
        for a, b in [((40, 70), (500, 70)), ((40, 100), (500, 100)),
                     ((120, 150), (500, 150)), ((40, 200), (500, 200)),
                     ((40, 70), (40, 200)), ((500, 70), (500, 200)),
                     ((300, 70), (300, 200)), ((120, 100), (120, 200)),
                     ((400, 100), (400, 150))]:
            page.draw_line(a, b)
        for pos, text, font in [
            ((50, 90), "Responsibility", "hebo"), ((310, 90), "Penalty", "hebo"),
            ((50, 125), "Staff", "helv"), ((130, 125), "Late 1-15 minutes", "helv"),
            ((310, 125), "A", "helv"), ((130, 175), "Late 61 minutes", "helv"),
            ((310, 175), "C + PNT", "helv"),
        ]:
            page.insert_text(pos, text, fontsize=10, fontname=font)
        return doc.tobytes()


class PdfTableTests(unittest.TestCase):
    def test_geometry_keeps_merged_cells_and_never_fills_empty_penalty(self):
        extraction = extract_pdf(table_pdf())
        self.assertEqual(len(extraction.tables), 1)
        table = extraction.tables[0]
        self.assertEqual(len(table.rows), 3)
        first, second = table.rows[1:]
        self.assertIn("Cột 1: Staff", first)
        self.assertIn("Cột 1: Staff", second)
        self.assertIn("Late 1-15 minutes", first)
        self.assertIn("Cột 3: A", first)
        self.assertNotIn("Cột 4:", first)
        self.assertIn("Cột 3–4: C + PNT", second)
        self.assertNotIn("Cột 3: A", second)
        self.assertEqual(table.row_headers, ("Responsibility | Penalty",) * 3)

    def test_upload_preparation_removes_flat_duplicate_and_keeps_whole_rows(self):
        text, chunks = prepare_pdf_document(extract_pdf(table_pdf()), size=200, overlap=30)
        self.assertEqual(text.count("Late 1-15 minutes"), 1)
        self.assertEqual(text.count("Late 61 minutes"), 1)
        for phrase, penalty in (("Late 1-15 minutes", "Cột 3: A"),
                                ("Late 61 minutes", "C + PNT")):
            found = [chunk for chunk in chunks if phrase in chunk.content]
            self.assertEqual(len(found), 1)
            self.assertIn(penalty, found[0].content)
            self.assertIn("Responsibility | Penalty", found[0].content)
            self.assertEqual(found[0].section_path, ("4. Policy",))

    def test_single_large_row_is_not_cut_from_its_penalty(self):
        text = "Dòng 1: Cột 1: " + "Condition " * 80 + " | Cột 2: Z"
        table = PdfTable(1, (40, 300, 500, 400), (text,))
        ex = PdfExtraction((line("4. Policy", bold=True),), 1, (), (table,))
        _, chunks = prepare_pdf_document(ex, size=200, overlap=30)
        self.assertEqual(len(chunks), 1)
        self.assertIn(text, chunks[0].content)

    def test_cross_page_header_and_audience_are_preserved_and_reset(self):
        header = PdfTable(1, (40, 700, 500, 730), ("Dòng 1: Header",), ("Policy | Penalty",))
        body = PdfTable(2, (40, 50, 500, 200), ("Dòng 1: Staff rule | A",), ("",))
        other = PdfTable(3, (40, 200, 500, 300), ("Dòng 1: Manager rule | C",), ("",))
        ex = PdfExtraction((
            line("Ordinary paragraph. " * 50, y=50),
            line("4. Policy", bold=True, y=200),
            line("a. Rules:", bold=True, y=250),
            line("❖ Staff:", bold=True, y=300),
            line("❖ Managers:", bold=True, y=100, page=3),
        ), 3, (), (header, body, other))
        _, chunks = prepare_pdf_document(ex)
        staff = next(c for c in chunks if "Staff rule" in c.content)
        manager = next(c for c in chunks if "Manager rule" in c.content)
        self.assertEqual(staff.section_path[-1], "❖ Staff:")
        self.assertEqual(manager.section_path[-1], "❖ Managers:")
        self.assertIn("Policy | Penalty", staff.content)
        self.assertNotIn("Policy | Penalty", manager.content)

    def test_table_only_extraction_still_has_chunks(self):
        table = PdfTable(1, (40, 50, 500, 200), ("Dòng 1: X | Y",))
        text, chunks = prepare_pdf_document(PdfExtraction((), 1, (), (table,)))
        self.assertIn("X | Y", text)
        self.assertEqual(len(chunks), 1)


if __name__ == "__main__":
    unittest.main()
