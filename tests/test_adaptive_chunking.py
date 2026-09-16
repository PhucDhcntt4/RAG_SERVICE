import re
import unittest

from app.adaptive_chunking import adaptive_chunk_text, recursive_split_text


class SequenceEmbedder:
    def embed(self, texts, query=False):
        del query
        base = ([1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 1.0])
        return [list(base[min(index, len(base) - 1)]) for index in range(len(texts))]


class AdaptiveChunkingTests(unittest.TestCase):
    def test_heading_is_a_hard_boundary(self):
        chunks, stats = adaptive_chunk_text(
            "# Mục A\nNội dung A.\n# Mục B\nNội dung B.",
            SequenceEmbedder(),
            max_chars=200,
            min_chars=50,
        )
        self.assertEqual([chunk.section_path for chunk in chunks], [("Mục A",), ("Mục B",)])
        self.assertEqual(stats.sections, 2)

    def test_semantic_break_is_applied_inside_one_heading(self):
        paragraphs = [
            "Chính sách bảo hành áp dụng cho sản phẩm mới và có hóa đơn.",
            "Khách hàng mang sản phẩm tới cửa hàng để được tiếp nhận.",
            "Cửa hàng tại Hà Nội nằm trên phố Bà Triệu và phố Thái Hà.",
            "Các chi nhánh hoạt động theo khung giờ được công bố.",
        ]
        chunks, stats = adaptive_chunk_text(
            "# Thông tin\n" + "\n\n".join(paragraphs),
            SequenceEmbedder(),
            max_chars=200,
            min_chars=50,
            breakpoint_percentile=80,
        )
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(chunk.section_path == ("Thông tin",) for chunk in chunks))
        self.assertIn("bảo hành", chunks[0].content)
        self.assertIn("Hà Nội", chunks[1].content)
        self.assertEqual(stats.semantic_sections, 1)
        self.assertGreater(stats.average_threshold, 0)

    def test_recursive_fallback_is_bounded_and_keeps_all_words(self):
        text = " ".join(f"token{index}" for index in range(200))
        parts = recursive_split_text(text, 100)
        self.assertTrue(all(0 < len(part) <= 100 for part in parts))
        self.assertEqual(
            re.findall(r"token\d+", " ".join(parts)),
            re.findall(r"token\d+", text),
        )

    def test_recursive_fallback_handles_text_without_boundaries(self):
        parts = recursive_split_text("x" * 501, 100)
        self.assertEqual("".join(parts), "x" * 501)
        self.assertEqual([len(part) for part in parts], [100, 100, 100, 100, 100, 1])

    def test_short_section_does_not_spend_semantic_embedding_call(self):
        class RejectEmbedder:
            def embed(self, texts, query=False):
                raise AssertionError((texts, query))

        chunks, stats = adaptive_chunk_text(
            "# Mục ngắn\nNội dung đủ ngắn.",
            RejectEmbedder(),
            max_chars=200,
            min_chars=50,
        )
        self.assertEqual(len(chunks), 1)
        self.assertEqual(stats.semantic_sections, 0)


if __name__ == "__main__":
    unittest.main()
