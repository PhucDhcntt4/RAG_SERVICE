import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.chunking import chunk_text
from app.config import Settings
from app.embeddings import EmbeddingError, GeminiEmbedder
from app.main import create_app
from app.models import DocumentRequest, SearchRequest
from app.repository import Repository
from app.service import InvalidDocument, KnowledgeService, ServiceBusy
from app.storage import StoredFile


def config(**overrides):
    return Settings(database_url="postgresql://unused/test", search_api_key="s" * 32,
                    admin_api_key="a" * 32, gemini_api_key="unused-test-key", **overrides)


def make_service(settings=None):
    settings = settings or config()
    repo = Mock()
    repo.search.return_value = []
    repo.replace.return_value = {"id": 1, "chunk_count": 1}
    repo.list_documents.return_value = []
    repo.get_document.return_value = None
    repo.delete.return_value = None
    repo.set_active.return_value = None
    embedder = Mock(provider="gemini", model="gemini-embedding-001", dimension=768)
    embedder.embed.side_effect = lambda texts, **kwargs: [[1.0] + [0.0] * 767 for _ in texts]
    storage = Mock()
    storage.save.return_value = StoredFile("a" * 32 + ".txt", "test.txt", "text/plain", 4, "text")
    storage.remove.return_value = True
    return KnowledgeService(settings, repo, embedder, storage=storage)


def sample_row(**overrides):
    return {"source_key": "warranty.txt", "title": "Bảo hành", "category": "warranty",
            "heading": "Thời gian", "chunk_index": 0, "similarity": 0.8,
            "content": "Nội dung kiểm thử, không phải chính sách thật.", **overrides}


class ChunkTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(chunk_text(" \n "), [])

    def test_headings_are_separate(self):
        chunks = chunk_text("# Nhóm A\nNội dung A\n# Nhóm B\nNội dung B")
        self.assertEqual([c.heading for c in chunks], ["Nhóm A", "Nhóm B"])
        self.assertEqual([c.index for c in chunks], [0, 1])

    def test_bounds_and_tail(self):
        chunks = chunk_text("abc " * 1000 + "END", 200, 30)
        self.assertTrue(all(0 < len(c.content) <= 200 for c in chunks))
        self.assertTrue(chunks[-1].content.endswith("END"))

    def test_no_whitespace_progress(self):
        chunks = chunk_text("x" * 2000, 200, 199)
        self.assertEqual(len(chunks), 1801)

    def test_invalid_overlap(self):
        with self.assertRaises(ValueError):
            chunk_text("abc", 200, 200)


class ServiceTests(unittest.TestCase):
    def test_ingest(self):
        svc = make_service()
        result = svc.ingest(DocumentRequest(source_key="a", title="A", text="# X\nXin chào"))
        self.assertEqual(result["id"], 1)
        svc.repository.replace.assert_called_once()
        self.assertIn("Xin chào", svc.embedder.embed.call_args.args[0][0])

    def test_failed_embedding_never_writes(self):
        svc = make_service()
        svc.embedder.embed.side_effect = EmbeddingError("Unavailable")
        with self.assertRaises(EmbeddingError):
            svc.ingest(DocumentRequest(source_key="a", title="A", text="Xin chào"))
        svc.repository.replace.assert_not_called()
        with svc.capacity():
            pass  # slot released after failure

    def test_empty_heading_only_rejected(self):
        svc = make_service()
        with self.assertRaises(InvalidDocument):
            svc.ingest(DocumentRequest(source_key="a", title="A", text="# Chỉ tiêu đề"))
        svc.embedder.embed.assert_not_called()

    def test_too_long_before_paid_call(self):
        svc = make_service(config(rag_max_document_chars=100))
        with self.assertRaises(InvalidDocument):
            svc.ingest(DocumentRequest(source_key="a", title="A", text="x" * 101))
        svc.embedder.embed.assert_not_called()

    def test_chunk_limit_before_paid_call(self):
        svc = make_service(config(rag_max_chunks=1, rag_chunk_size=200, rag_chunk_overlap=0))
        with self.assertRaises(InvalidDocument):
            svc.ingest(DocumentRequest(source_key="a", title="A", text="x" * 500))
        svc.embedder.embed.assert_not_called()

    def test_no_results(self):
        result = make_service().search(SearchRequest(query="Hỏi bảo hành"))
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "knowledge_not_found")
        self.assertEqual(result["sources"], [])

    def test_categories_forwarded_and_sources_returned(self):
        svc = make_service()
        svc.repository.search.return_value = [sample_row()]
        result = svc.search(SearchRequest(query="Hỏi bảo hành", categories=["warranty"], top_k=3))
        self.assertTrue(result["success"])
        self.assertEqual(svc.repository.search.call_args.args[2:], (["warranty"], 0.45, 3))
        self.assertEqual(result["sources"][0]["source_key"], "warranty.txt")

    def test_context_limit_includes_headers_and_separators(self):
        svc = make_service(config(rag_max_context_chars=500))
        svc.repository.search.return_value = [sample_row(content="a" * 300), sample_row(content="b" * 400)]
        result = svc.search(SearchRequest(query="abc"))
        self.assertEqual(len(result["content"]), 500)
        self.assertEqual(len(result["sources"]), 2)

    def test_busy(self):
        svc = make_service(config(rag_max_concurrent_requests=1))
        with svc.capacity():
            with self.assertRaises(ServiceBusy):
                svc.search(SearchRequest(query="abc"))
        svc.embedder.embed.assert_not_called()

    def test_file_validation(self):
        svc = make_service()
        self.assertEqual(svc.extract("test.md", "Tiếng Việt".encode()), "Tiếng Việt")
        for name, data in [("a.exe", b"abc"), ("a.txt", b"\xff"), ("a.pdf", b"invalid")]:
            with self.subTest(name=name), self.assertRaises(InvalidDocument):
                svc.extract(name, data)


class APITests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        self.client = TestClient(create_app(self.svc.settings, self.svc))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.search_headers = {"Authorization": "Bearer " + "s" * 32}
        self.admin_headers = {"Authorization": "Bearer " + "a" * 32}

    def test_health_live(self):
        self.assertEqual(self.client.get("/health/live").status_code, 200)

    def test_dashboard_assets_are_public_but_documents_require_admin(self):
        for path in ("/", "/admin", "/assets/admin.js", "/assets/admin.css"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            for secret in ("a" * 32, "s" * 32, "unused-test-key", "postgresql://unused/test"):
                self.assertNotIn(secret, response.text)
        page = self.client.get("/")
        self.assertIn("text/html", page.headers["content-type"])
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        self.assertEqual(self.client.get("/api/v1/documents").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/documents", headers=self.search_headers).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/documents", headers=self.admin_headers).status_code, 200)

    def test_static_mount_cannot_expose_project_configuration(self):
        for path in ("/assets/.env", "/assets/%2e%2e/config.py", "/assets/%2e%2e/%2e%2e/.env"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_missing_and_bad_keys(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            response = self.client.post("/api/v1/knowledge/search", json={"query": "abc"}, headers=headers)
            self.assertEqual(response.status_code, 401)
        self.svc.embedder.embed.assert_not_called()

    def test_search_key_cannot_manage_documents(self):
        for method, path in [("get", "/api/v1/documents"), ("put", "/api/v1/documents"),
                             ("post", "/api/v1/documents/upload"), ("delete", "/api/v1/documents/1"),
                             ("patch", "/api/v1/documents/1")]:
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)(path, headers=self.search_headers).status_code, 403)

    def test_valid_search_and_timing(self):
        response = self.client.post("/api/v1/knowledge/search", json={"query": "abc"}, headers=self.search_headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("elapsed_ms", response.json())
        self.assertIn("X-Response-Time-Ms", response.headers)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_invalid_query_and_unknown_fields(self):
        for body in ({"query": " "}, {"query": "a", "top_k": 100}, {"query": "a", "unexpected": 1}):
            self.assertEqual(self.client.post("/api/v1/knowledge/search", json=body,
                                              headers=self.search_headers).status_code, 422)

    def test_admin_upsert(self):
        response = self.client.put("/api/v1/documents", headers=self.admin_headers,
                                   json={"source_key": "a.txt", "title": "A", "text": "Kiểm thử"})
        self.assertEqual(response.status_code, 200)
        self.svc.repository.replace.assert_called_once()

    def test_admin_upload(self):
        response = self.client.post("/api/v1/documents/upload", headers=self.admin_headers,
                                    data={"source_key": "a.txt", "title": "A", "category": "size_guide"},
                                    files={"file": ("a.txt", "Kiểm thử".encode(), "text/plain")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.svc.repository.replace.call_args.args[0].category, "size_guide")
        self.svc.storage.save.assert_called_once_with("Kiểm thử".encode(), "a.txt", origin="upload")

    def test_download_preserves_file_bytes_and_requires_admin(self):
        self.svc.repository.get_document.return_value = {
            "file_storage_key": "a" * 32 + ".txt", "file_name": "Cửa hàng.txt", "file_mime_type": "text/plain",
        }
        raw = b"\xef\xbb\xbfOriginal file\r\n"
        self.svc.storage.read.return_value = raw
        path = "/api/v1/documents/1/download"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers=self.search_headers).status_code, 403)
        self.svc.storage.read.assert_not_called()
        result = self.client.get(path, headers=self.admin_headers)
        self.assertEqual(result.content, raw)
        self.assertIn("attachment;", result.headers["content-disposition"])
        self.assertEqual(result.headers["x-content-type-options"], "nosniff")

    def test_missing_local_file_is_404(self):
        self.svc.repository.get_document.return_value = {"file_storage_key": None}
        self.assertEqual(self.client.get("/api/v1/documents/1/download", headers=self.admin_headers).status_code, 404)
        self.svc.repository.get_document.return_value = {"file_storage_key": "a" * 32 + ".txt"}
        self.svc.storage.read.side_effect = FileNotFoundError()
        self.assertEqual(self.client.get("/api/v1/documents/1/download", headers=self.admin_headers).status_code, 404)

    def test_delete_reports_local_cleanup_failure(self):
        self.svc.repository.delete.return_value = {"id": 1, "file_storage_key": "a" * 32 + ".txt"}
        self.svc.storage.remove.return_value = False
        response = self.client.delete("/api/v1/documents/1", headers=self.admin_headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["local_file_deleted"])
        self.assertIn("warning", response.json())

    def test_invalid_upload_form(self):
        response = self.client.post("/api/v1/documents/upload", headers=self.admin_headers,
                                    data={"source_key": " ", "title": "A"},
                                    files={"file": ("a.txt", b"Hello", "text/plain")})
        self.assertEqual(response.status_code, 422)

    def test_missing_document(self):
        self.assertEqual(self.client.get("/api/v1/documents/999", headers=self.admin_headers).status_code, 404)

    def test_embedding_failure_is_503_not_empty_knowledge(self):
        self.svc.embedder.embed.side_effect = EmbeddingError("secret provider detail")
        response = self.client.post("/api/v1/knowledge/search", json={"query": "abc"}, headers=self.search_headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", response.text)

    def test_readiness_calls_db_not_embedding(self):
        response = self.client.get("/api/v1/health/ready", headers=self.search_headers)
        self.assertEqual(response.status_code, 200)
        self.svc.repository.ready.assert_called_once()
        self.svc.embedder.embed.assert_not_called()

    def test_oversized_body(self):
        self.svc.settings.rag_max_document_chars = 100
        self.svc.settings.rag_max_upload_bytes = 1024
        response = self.client.post("/api/v1/knowledge/search", content=b"x" * 70000, headers=self.search_headers)
        self.assertEqual(response.status_code, 413)


class EmbeddingTests(unittest.TestCase):
    def test_batching_task_and_normalization(self):
        with patch("app.embeddings.genai.Client") as client:
            client.return_value.models.embed_content.side_effect = lambda **kwargs: SimpleNamespace(
                embeddings=[SimpleNamespace(values=[2.0] + [0.0] * 767) for _ in kwargs["contents"]])
            embedder = GeminiEmbedder(config())
            vectors = embedder.embed(["abc"] * 33)
            self.assertEqual(len(vectors), 33)
            self.assertEqual(vectors[0][0], 1.0)
            self.assertEqual(client.return_value.models.embed_content.call_count, 2)
            embedder.embed(["abc"], query=True)
            self.assertEqual(client.return_value.models.embed_content.call_args.kwargs["config"].task_type,
                             "QUESTION_ANSWERING")

    def test_reject_invalid_vectors(self):
        for vector in ([0.0] * 768, [float("nan")] * 768, [1.0] * 767):
            with self.subTest(size=len(vector)), patch("app.embeddings.genai.Client") as client:
                client.return_value.models.embed_content.return_value = SimpleNamespace(
                    embeddings=[SimpleNamespace(values=vector)])
                with self.assertRaises(EmbeddingError):
                    GeminiEmbedder(config()).embed(["abc"])


class ConfigTests(unittest.TestCase):
    def test_invalid_keys_and_overlap(self):
        data = config().model_dump()
        for update in ({"admin_api_key": "s" * 32}, {"search_api_key": "short"}, {"rag_chunk_overlap": 1200}):
            with self.subTest(update=list(update)), self.assertRaises(ValidationError):
                Settings.model_validate({**data, **update})

    def test_repr_masks_secrets(self):
        self.assertNotIn("unused-test-key", repr(config()))


class RepositoryTests(unittest.TestCase):
    def test_search_sql_has_metadata_and_category_filters(self):
        repo = Repository(config())
        with patch.object(repo, "connection") as connection:
            conn = connection.return_value.__enter__.return_value
            conn.execute.return_value.fetchall.return_value = []
            repo.search([1.0] + [0.0] * 767, make_service().embedder, ["warranty"], 0.45, 5)
            sql, params = conn.execute.call_args.args
            self.assertIn("d.is_active", sql)
            self.assertIn("d.embedding_model=%s", sql)
            self.assertIn("d.category = ANY", sql)
            self.assertEqual(params[4], ["warranty"])

    def test_replace_failure_exits_transaction_with_exception(self):
        repo = Repository(config())
        with patch.object(repo, "connection") as connection:
            context = connection.return_value
            conn = context.__enter__.return_value
            conn.execute.return_value.fetchone.return_value = {"id": 1}
            conn.cursor.return_value.__enter__.return_value.executemany.side_effect = RuntimeError("insert failed")
            with self.assertRaises(RuntimeError):
                repo.replace(DocumentRequest(source_key="a", title="A", text="Hello"),
                             chunk_text("Hello"), [[1.0] + [0.0] * 767], make_service().embedder)
            self.assertIs(context.__exit__.call_args.args[0], RuntimeError)


if __name__ == "__main__":
    unittest.main()
