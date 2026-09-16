import unittest

from app.bm25 import BM25Index
from app.chunking import chunk_text
from app.models import DocumentRequest, SearchRequest
from app.search_text import build_search_text
from tests.test_service import config, make_service, sample_row


class SectionPathTests(unittest.TestCase):
    def test_siblings_and_new_root_do_not_inherit_previous_branch(self):
        chunks = chunk_text(
            "Giới thiệu\n# TP.HCM\n## A\nNội dung A\n"
            "## B\nNội dung B\n# Hà Nội\n## C\nNội dung C"
        )
        self.assertEqual([c.section_path for c in chunks], [
            (), ("TP.HCM", "A"), ("TP.HCM", "B"), ("Hà Nội", "C"),
        ])
        self.assertEqual([c.index for c in chunks], list(range(4)))

    def test_long_section_keeps_parent_path_and_tail(self):
        chunks = chunk_text("# TP.HCM\n### A\n" + "abc " * 400 + "END", 200, 30)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c.section_path == ("TP.HCM", "A") for c in chunks))
        self.assertTrue(all(len(c.content) <= 200 for c in chunks))
        self.assertTrue(chunks[-1].content.endswith("END"))

    def test_formatter_preserves_legacy_text_and_does_not_repeat_leaf(self):
        for path in (None, [], ()):
            self.assertEqual(build_search_text("T", "Leaf", "Body", path), "T\nLeaf\nBody")
        self.assertEqual(build_search_text("T", None, "Body"), "T\n\nBody")
        self.assertEqual(
            build_search_text("T", "Leaf", "Body", ("Parent", "Leaf")),
            "T\nParent > Leaf\nBody",
        )

    def test_bm25_finds_parent_only_term_and_respects_category(self):
        chunks = chunk_text("# TP.HCM\n## A\nĐịa chỉ A\n# Hà Nội\n## B\nĐịa chỉ B")
        rows = [dict(chunk_id=c.index + 1, title="Fixture", category="store",
                     heading=c.heading, content=c.content, section_path=list(c.section_path))
                for c in chunks]
        index = BM25Index(rows)
        self.assertNotIn("HCM", rows[0]["content"])
        self.assertEqual([r["chunk_id"] for r in index.search("HCM", categories=["store"])], [1])
        self.assertEqual(index.search("HCM", categories=["warranty"]), [])

    def test_ingest_embeds_parent_and_passes_structure_to_repository(self):
        service = make_service()
        service.ingest(DocumentRequest(
            source_key="fixture/hierarchy", title="Fixture",
            text="# TP.HCM\n## A\nĐịa chỉ A",
        ))
        self.assertEqual(service.embedder.embed.call_args.args[0], ["Fixture\nTP.HCM > A\nĐịa chỉ A"])
        chunks = service.repository.replace.call_args.args[1]
        self.assertEqual(chunks[0].section_path, ("TP.HCM", "A"))

    def test_context_keeps_parent_citations_and_character_budget(self):
        for numbered in (False, True):
            with self.subTest(numbered=numbered):
                service = make_service(config(rag_max_context_chars=500))
                service.repository.search.return_value = [sample_row(
                    title="Fixture", heading="A", section_path=["TP.HCM", "A"],
                    content="x" * 1000,
                )]
                result = service.search(SearchRequest(query="HCM"), numbered_sources=numbered)
                self.assertIn("[Nguồn: Fixture > TP.HCM > A]", result["content"])
                self.assertEqual(len(result["content"]), 500)
                self.assertEqual(result["content"].startswith("[S1]"), numbered)
                self.assertEqual(result["sources"][0]["heading"], "A")


if __name__ == "__main__":
    unittest.main()
