import unittest

from app.models import DocumentRequest, SearchRequest, SearchResponse
from tests.test_service import make_service, sample_row


def lexical_row(chunk_id, content, *, category="company"):
    row = sample_row(
        chunk_id=chunk_id,
        document_id=chunk_id,
        source_key=f"doc-{chunk_id}",
        title=f"Document {chunk_id}",
        category=category,
        content=content,
    )
    row.pop("similarity", None)
    return row


class HybridApiTests(unittest.TestCase):
    def test_service_fuses_bm25_and_vector_before_top_k(self):
        service = make_service()
        service.repository.list_search_chunks.return_value = [
            lexical_row(1, "alpha alpha exact"),
            lexical_row(2, "alpha related"),
            lexical_row(3, "semantic only"),
        ]
        service.refresh_bm25()
        service.repository.search.return_value = [
            sample_row(chunk_id=2, document_id=2, source_key="doc-2",
                       title="Document 2", category="company",
                       content="alpha related", similarity=.95),
            sample_row(chunk_id=3, document_id=3, source_key="doc-3",
                       title="Document 3", category="company",
                       content="semantic only", similarity=.90),
        ]

        result = service.search(SearchRequest(query="alpha", top_k=3))

        self.assertEqual(result["sources"][0]["source_key"], "doc-2")
        self.assertEqual(result["sources"][0]["bm25_rank"], 2)
        self.assertEqual(result["sources"][0]["vector_rank"], 1)
        self.assertGreater(result["sources"][0]["rrf_score"], 0)
        self.assertEqual(service.repository.search.call_args.args[4], 20)

    def test_bm25_only_match_is_valid_and_respects_category(self):
        service = make_service()
        service.repository.list_search_chunks.return_value = [
            lexical_row(1, "ma-chinh-xac-987", category="store"),
            lexical_row(2, "ma-chinh-xac-987", category="warranty"),
        ]
        service.refresh_bm25()
        service.repository.search.return_value = []

        result = service.search(SearchRequest(
            query="ma-chinh-xac-987", categories=["store"], top_k=5,
        ))

        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["category"], "store")
        self.assertIsNone(result["sources"][0]["similarity"])
        self.assertEqual(result["sources"][0]["bm25_rank"], 1)
        SearchResponse.model_validate(result)

    def test_document_mutations_refresh_bm25(self):
        service = make_service()
        service.repository.list_search_chunks.return_value = []
        service.repository.delete.return_value = {"id": 1, "file_storage_key": None}
        service.repository.set_active.return_value = {"id": 1, "is_active": False}

        service.ingest(DocumentRequest(source_key="a", title="A", text="Content"))
        service.delete(1)
        service.set_active(1, False)

        self.assertEqual(service.repository.list_search_chunks.call_count, 3)


if __name__ == "__main__":
    unittest.main()
