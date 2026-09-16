import unittest
from dataclasses import replace

from app.pdf_extract import PdfExtraction, PdfLine, PdfSpan
from app.pdf_structure import detect_headings, prepare_lines


def line(text, *, bold=False, size=13, page=1, x=50, y=100, width=200):
    bbox = (x, y, x + width, y + size)
    span = PdfSpan(text, "Fixture", size, bold, bbox)
    return PdfLine(text, page, 0, bbox, 600, 800, (span,))


def detect(*lines):
    # A realistic body majority keeps heading fonts from becoming the baseline.
    body = line("Ordinary paragraph. " * 100, y=500)
    return detect_headings(PdfExtraction((body, *lines), 2, ()))


class PdfStructureTests(unittest.TestCase):
    def test_hierarchy_survives_page_break_and_resets_at_next_section(self):
        result = detect(
            line("BUILDING RULES", bold=True, size=22),
            line("Opening hours:", bold=True),
            line("I. Security:", bold=True),
            line("1.1. Entry:", bold=True),
            line("a. Staff:", bold=True),
            line("b. Visitors:", bold=True, page=2, y=20),
            line("II. Assets:", bold=True, page=2),
            line("2.1. Keys:", bold=True, page=2),
        )
        headings = [item for item in result if item.level is not None]
        self.assertEqual([item.level for item in headings], [1, 2, 2, 3, 4, 4, 2, 3])
        self.assertEqual(headings[5].line.page, 2)

    def test_detached_prefix_merges_only_with_adjacent_same_row_title(self):
        prefix = line("1.1.", bold=True, width=20)
        title = line("Entry rules:", bold=True, x=85)
        merged = prepare_lines([prefix, title], 13)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].text, "1.1. Entry rules:")
        self.assertEqual(merged[0].spans, prefix.spans + title.spans)
        self.assertEqual(merged[0].bbox, (50, 100, 285, 113))
        for other in (
            line("Entry rules:", bold=True, page=2, x=85),
            line("Entry rules:", bold=True, x=85, y=120),
            line("Entry rules:", bold=True, x=300),
            line("Regular text", x=85),
        ):
            with self.subTest(other=other):
                self.assertEqual(prepare_lines([prefix, other], 13), [prefix, other])

    def test_only_matching_bottom_page_number_is_removed(self):
        footer = line("2", size=11, page=2, y=760)
        content_number = line("2", page=2, y=100)
        different_number = line("229", page=2, y=760)
        lines = [content_number, different_number, footer]
        self.assertEqual(prepare_lines(lines, 13), lines[:2])

    def test_bold_bullets_and_body_continuations_are_not_headings(self):
        result = detect(
            line("I. Rules:", bold=True),
            line("- All employees must check in:", bold=True),
            line("including overtime:", bold=True),
            line("1. A regular numbered list", bold=False),
        )
        self.assertEqual([item.line.text for item in result if item.level], ["I. Rules:"])

    def test_bold_numbered_time_phrase_is_body(self):
        result = detect(
            line("1. Savings:", bold=True),
            line("10 tây đến 12 tây hằng tháng:", bold=True),
        )
        self.assertEqual([item.line.text for item in result if item.level], ["1. Savings:"])

    def test_inline_heading_preserves_body_spans_and_location(self):
        heading = line("a. Usage: ", bold=True, page=2, width=80)
        body = line("The building has seven floors.", page=2, x=130)
        mixed = replace(heading, text=heading.text + body.text,
                        spans=heading.spans + body.spans,
                        bbox=(50, 100, 330, 113))
        result = detect(line("2.1. Departments:", bold=True), mixed)
        self.assertEqual(result[-2].level, 4)
        self.assertEqual(result[-2].line.text, "a. Usage:")
        self.assertEqual(result[-2].line.bbox, heading.bbox)
        self.assertIsNone(result[-1].level)
        self.assertEqual(result[-1].line, body)
        self.assertEqual(result[-2].line.spans + result[-1].line.spans, mixed.spans)
        self.assertEqual(result[-2].line.text + " " + result[-1].line.text, mixed.text)

    def test_inline_emphasis_without_heading_context_keeps_original_line(self):
        label = line("a. Usage: ", bold=True)
        body = line("Description.")
        mixed = replace(label, text=label.text + body.text,
                        spans=label.spans + body.spans)
        result = detect(mixed)
        self.assertIsNone(result[-1].level)
        self.assertEqual(result[-1].line, mixed)

    def test_empty_extraction(self):
        self.assertEqual(detect_headings(PdfExtraction((), 1, (1,))), [])

    def test_named_part_is_a_top_level_heading(self):
        result = detect(
            line("HANDBOOK", bold=True, size=22),
            line("PHẦN III: CHÍNH SÁCH PHÚC LỢI", bold=True, size=20, page=2),
            line("1. CHÍNH SÁCH TIẾT KIỆM", bold=True, page=2),
            line("1.1. Mục đích:", bold=True, page=2),
        )
        headings = [item for item in result if item.level is not None]
        self.assertEqual([item.level for item in headings], [1, 1, 2, 3])
        self.assertEqual(headings[1].reason, "document_part")

    def test_smaller_table_of_contents_part_does_not_create_scope(self):
        result = detect(
            line("PHẦN III: CHÍNH SÁCH PHÚC LỢI", bold=True, size=14, page=1),
            line("PHẦN III: CHÍNH SÁCH PHÚC LỢI", bold=True, size=24, page=2),
        )
        part_items = [item for item in result if item.reason == "document_part"]
        self.assertEqual(len(part_items), 1)
        self.assertEqual(part_items[0].line.page, 2)


if __name__ == "__main__":
    unittest.main()
