import unittest

from fastapi.testclient import TestClient

from app.chunking import chunk_text
from app.main import create_app
from app.models import DocumentRequest
from app.text_structure import detect_text_headings
from tests.test_service import make_service


class TextStructureTests(unittest.TestCase):
    def test_numbered_hierarchy_and_sibling_reset(self):
        text = (
            "Giới thiệu.\nI. QUY ĐỊNH CHUNG\n1.1. Thời gian làm việc\n"
            "Phạm vi áp dụng.\na. Nhân viên chính thức\nNội dung A.\n"
            "b. Nhân viên thời vụ\nNội dung B.\n"
            "II. QUẢN LÝ TÀI SẢN\n2.1. Sử dụng thiết bị\nNội dung C."
        )
        chunks = chunk_text(text)
        self.assertEqual([chunk.section_path for chunk in chunks], [
            (), ("I. QUY ĐỊNH CHUNG", "1.1. Thời gian làm việc"),
            ("I. QUY ĐỊNH CHUNG", "1.1. Thời gian làm việc", "a. Nhân viên chính thức"),
            ("I. QUY ĐỊNH CHUNG", "1.1. Thời gian làm việc", "b. Nhân viên thời vụ"),
            ("II. QUẢN LÝ TÀI SẢN", "2.1. Sử dụng thiết bị"),
        ])
        self.assertEqual([chunk.content for chunk in chunks], [
            "Giới thiệu.", "Phạm vi áp dụng.", "Nội dung A.", "Nội dung B.", "Nội dung C.",
        ])

    def test_decimal_outline_without_roman_parent(self):
        chunks = chunk_text("1.1. Thiết bị\nA.\n1.1.1. Máy in\nB.\n1.2. Chìa khóa\nC.")
        self.assertEqual([chunk.section_path for chunk in chunks], [
            ("1.1. Thiết bị",), ("1.1. Thiết bị", "1.1.1. Máy in"), ("1.2. Chìa khóa",),
        ])

    def test_uppercase_regions_keep_numbered_addresses_as_content(self):
        text = (
            "TP. HỒ CHÍ MINH\n\n"
            "1. 227-229 Hai Bà Trưng, P. Xuân Hòa.\n"
            "2. 162-164 Quang Trung, P. Gò Vấp.\n\n"
            "HÀ NỘI\n\n1. 222 Bà Triệu, P. Hai Bà Trưng."
        )
        chunks = chunk_text(text)
        self.assertEqual([c.heading for c in chunks], ["TP. HỒ CHÍ MINH", "HÀ NỘI"])
        self.assertIn("1. 227-229", chunks[0].content)
        self.assertIn("2. 162-164", chunks[0].content)
        self.assertIn("1. 222 Bà Triệu", chunks[1].content)

    def test_uppercase_document_title_is_parent_of_roman_outline(self):
        text = "NỘI QUY CÔNG TY\n\nI. Phạm vi\n1.1. Nhân viên\nNội dung."
        self.assertEqual(chunk_text(text)[0].section_path, (
            "NỘI QUY CÔNG TY", "I. Phạm vi", "1.1. Nhân viên",
        ))

    def test_warning_numbered_list_and_letter_without_parent_stay_body(self):
        text = (
            "LƯU Ý\n\nKHÔNG ĐƯỢC CHIA SẺ MẬT KHẨU\n\n"
            "1. Đăng nhập\n2. Chọn tài liệu\na. Nhân viên\n"
            "08.00 - 22.00\n1.1. 227-229 Hai Bà Trưng\n"
            "I. Đây là một câu bình thường."
        )
        self.assertTrue(all(item.level is None for item in detect_text_headings(text)))
        self.assertEqual(chunk_text(text)[0].content, text)

    def test_single_uppercase_label_without_outline_is_conservative(self):
        text = "GHI NHỚ\n\nMột câu cần giữ nguyên."
        self.assertTrue(all(item.level is None for item in detect_text_headings(text)))

    def test_markdown_remains_authoritative_in_mixed_documents(self):
        text = "# Quy trình\n1.1. Bước trong danh sách\nNội dung.\n## Ghi chú\nKhác."
        chunks = chunk_text(text)
        self.assertEqual([c.section_path for c in chunks], [("Quy trình",), ("Quy trình", "Ghi chú")])
        self.assertIn("1.1. Bước trong danh sách", chunks[0].content)

    def test_long_section_keeps_path_and_final_content(self):
        chunks = chunk_text("I. Phạm vi\n1.1. Nhân viên\n" + "nội dung " * 100 + "END", 200, 30)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c.section_path == ("I. Phạm vi", "1.1. Nhân viên") for c in chunks))
        self.assertTrue(all(len(c.content) <= 200 for c in chunks))
        self.assertEqual([c.index for c in chunks], list(range(len(chunks))))
        self.assertTrue(chunks[-1].content.endswith("END"))

    def test_crlf_and_reason_codes(self):
        lines = detect_text_headings("I. Phạm vi\r\n1.1. Nhân viên\ra. Chính thức\nNội dung.")
        self.assertEqual([item.reason for item in lines], [
            "roman_number", "section_number", "letter_section", "body",
        ])

    def test_upload_passes_natural_paths_to_embedding_and_repository(self):
        service = make_service()
        raw = "I. Phạm vi\r\n1.1. Nhân viên\r\nNội dung kiểm thử.".encode("utf-8-sig")
        with TestClient(create_app(service.settings, service)) as client:
            response = client.post(
                "/api/v1/documents/upload",
                headers={"Authorization": "Bearer " + "a" * 32},
                data={"source_key": "fixture/natural", "title": "Fixture", "category": "company"},
                files={"file": ("rules.TXT", raw, "text/plain")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        document, chunks = service.repository.replace.call_args.args[:2]
        self.assertEqual(document.category, "company")
        self.assertEqual(chunks[0].section_path, ("I. Phạm vi", "1.1. Nhân viên"))
        self.assertEqual(service.embedder.embed.call_args.args[0], [
            "Fixture\nI. Phạm vi > 1.1. Nhân viên\nNội dung kiểm thử.",
        ])
        service.storage.save.assert_called_once_with(raw, "rules.TXT", origin="upload")

    def test_pdf_ingest_does_not_apply_text_heuristics_to_extracted_lines(self):
        service = make_service()
        service.ingest(
            DocumentRequest(source_key="fixture/pdf", title="Fixture", text="I. Phạm vi\nNội dung."),
            original_data=b"fixture", original_filename="rules.PDF",
        )
        chunks = service.repository.replace.call_args.args[1]
        self.assertEqual(chunks[0].section_path, ())
        self.assertEqual(chunks[0].content, "I. Phạm vi\nNội dung.")


if __name__ == "__main__":
    unittest.main()
