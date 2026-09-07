"""Opt-in integration tests on a NEW temporary PostgreSQL cluster, never a configured DB."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest

from fastapi.testclient import TestClient
import psycopg

from app.chunking import chunk_text
from app.config import ROOT, Settings
from app.main import create_app
from app.models import DocumentRequest, SearchRequest
from app.repository import Repository
from app.service import KnowledgeService
from app.storage import LocalFileStore
from app.backfill_files import backfill
from app.migrate_file_names import migrate


class FakeEmbedder:
    provider = "gemini"
    model = "gemini-embedding-001"
    dimension = 768

    def embed(self, texts, **kwargs):
        return [[1.0] + [0.0] * 767 for _ in texts]


@unittest.skipUnless(os.getenv("RAG_TEST_REAL_DB") == "1", "Set RAG_TEST_REAL_DB=1 for isolated PostgreSQL tests")
class PostgreSQLTests(unittest.TestCase):
    @classmethod
    def command(cls, *args):
        # On Windows, server descendants can keep PIPE handles open after pg_ctl exits.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(args, stdout=output, stderr=subprocess.STDOUT, timeout=45,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            output.seek(0)
            detail_output = output.read().decode(errors="replace")
        if result.returncode:
            log = Path(cls.temp.name) / "postgres.log"
            detail = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            raise RuntimeError(detail_output + detail)
        return result

    @classmethod
    def setUpClass(cls):
        for program in ("initdb", "pg_ctl"):
            if not shutil.which(program):
                raise unittest.SkipTest(f"{program} not available on PATH")
        cls.temp = tempfile.TemporaryDirectory(prefix="donghai_rag_test_")
        cls.data = Path(cls.temp.name).resolve() / "pgdata"
        cls.started = False
        cls.addClassCleanup(cls.cleanup_cluster)
        cls.command(shutil.which("initdb"), "-D", str(cls.data), "--auth=trust", "--username=rag_test",
                    "--encoding=UTF8", "--no-locale")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        # Trust authentication only in an ephemeral localhost-only test cluster.
        cls.command(shutil.which("pg_ctl"), "-D", str(cls.data), "-l", str(Path(cls.temp.name) / "postgres.log"),
                    "-o", f"-h 127.0.0.1 -p {port}", "-w", "-t", "20", "start")
        cls.started = True
        cls.settings = Settings(database_url=f"postgresql://rag_test@127.0.0.1:{port}/postgres",
                                search_api_key="s" * 32, admin_api_key="a" * 32, gemini_api_key="fake")
        with psycopg.connect(cls.settings.database_url.get_secret_value()) as conn:
            conn.execute((ROOT / "database/001_init.sql").read_text(encoding="utf-8"))
        cls.repo = Repository(cls.settings)

    @classmethod
    def cleanup_cluster(cls):
        # Only stop the server we created; do not use machine-wide service commands.
        if cls.started or (cls.data / "postmaster.pid").exists():
            cls.command(shutil.which("pg_ctl"), "-D", str(cls.data), "-m", "immediate", "-w", "stop")
        cls.temp.cleanup()

    def setUp(self):
        self.service = KnowledgeService(self.settings, self.repo, FakeEmbedder(),
                                        storage=LocalFileStore(Path(self.temp.name) / "files"))

    def test_database_workflow(self):
        doc = DocumentRequest(source_key="integration/a", title="Bảo hành", category="warranty", text="Nội dung cũ")
        result = self.service.ingest(doc)
        document_id = result["id"]
        self.repo.ready()
        found = self.service.search(SearchRequest(query="Bảo hành", categories=["warranty"]))
        self.assertTrue(found["success"])
        self.assertIn("Nội dung cũ", found["content"])
        self.assertFalse(self.service.search(SearchRequest(query="Bảo hành", categories=["store"]))["success"])

        self.repo.set_active(document_id, False)
        self.assertFalse(self.service.search(SearchRequest(query="Bảo hành"))["success"])
        updated = doc.model_copy(update={"text": "Nội dung mới"})
        self.assertFalse(self.service.ingest(updated)["is_active"])
        self.repo.set_active(document_id, True)
        self.assertIn("Nội dung mới", self.service.search(SearchRequest(query="Bảo hành"))["content"])
        self.assertEqual(self.repo.get_document(document_id)["source_text"], "Nội dung mới")
        self.assertEqual(self.repo.list_documents(50, 0)[0]["chunk_count"], 1)
        self.repo.delete(document_id)
        self.assertIsNone(self.repo.get_document(document_id))
        with self.repo.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM rag_service.chunks WHERE document_id=%s",
                                          (document_id,)).fetchone()["n"], 0)

    def test_real_rollback_preserves_old_content(self):
        doc = DocumentRequest(source_key="integration/rollback", title="A", text="Original")
        result = self.service.ingest(doc)
        with self.assertRaises(psycopg.Error):
            self.repo.replace(doc.model_copy(update={"text": "Changed"}), chunk_text("Changed"),
                              [[1.0, 0.0]], FakeEmbedder())  # invalid dimension AFTER DELETE
        self.assertEqual(self.repo.get_document(result["id"])["source_text"], "Original")
        self.assertIn("Original", self.service.search(SearchRequest(query="a"))["content"])
        self.repo.delete(result["id"])

    def test_local_file_replacement_download_and_deletion(self):
        doc = DocumentRequest(source_key="integration/files", title="File test", text="Old text")
        first = self.service.ingest(doc)
        old_key = self.repo.get_document(first["id"])["file_storage_key"]
        raw = b"\xef\xbb\xbfNew text\r\n"
        updated = self.service.ingest(doc.model_copy(update={"text": "New text"}),
                                      original_data=raw, original_filename="original.txt")
        row = self.repo.get_document(updated["id"])
        self.assertFalse(self.service.storage.path(old_key).exists())
        self.assertEqual(self.service.storage.read(row["file_storage_key"]), raw)
        self.assertEqual(row["file_origin"], "upload")
        headers = {"Authorization": "Bearer " + "a" * 32}
        with TestClient(create_app(self.settings, self.service)) as client:
            self.assertEqual(client.get(f"/api/v1/documents/{row['id']}/download", headers=headers).content, raw)
            self.assertTrue(client.delete(f"/api/v1/documents/{row['id']}", headers=headers).json()["local_file_deleted"])
        self.assertFalse(self.service.storage.path(row["file_storage_key"]).exists())
        self.assertIsNone(self.repo.get_document(row["id"]))

    def test_backfill_exports_legacy_text_once_without_embedding(self):
        doc = DocumentRequest(source_key="integration/legacy", title="Legacy", text="Existing source text")
        result = self.repo.replace(doc, chunk_text(doc.text), [[1.0] + [0.0] * 767], FakeEmbedder())
        self.assertIsNone(self.repo.get_document(result["id"])["file_storage_key"])
        self.assertGreaterEqual(backfill(self.repo, self.service.storage), 1)
        row = self.repo.get_document(result["id"])
        self.assertEqual(row["file_origin"], "recovered_text")
        self.assertEqual(self.service.storage.read(row["file_storage_key"]), doc.text.encode())
        self.assertEqual(backfill(self.repo, self.service.storage), 0)
        self.service.delete(row["id"])

    def test_legacy_filename_migration_preserves_bytes_and_db_reference(self):
        doc = DocumentRequest(source_key="integration/rename", title="Store", text="Existing source text")
        result = self.service.ingest(doc)
        row = self.repo.get_document(result["id"])
        previous_path = self.service.storage.path(row["file_storage_key"])
        legacy_key = "b" * 32 + ".txt"
        legacy_path = self.service.storage.path(legacy_key)
        previous_path.rename(legacy_path)
        with self.repo.connection() as conn:
            conn.execute("UPDATE rag_service.documents SET file_storage_key=%s WHERE id=%s", (legacy_key, row["id"]))
        self.assertEqual(migrate(self.repo, self.service.storage), 1)
        updated = self.repo.get_document(row["id"])
        self.assertEqual(self.service.storage.path(updated["file_storage_key"]).name, "Store.txt")
        self.assertEqual(self.service.storage.read(updated["file_storage_key"]), doc.text.encode())
        self.assertFalse(legacy_path.exists())
        self.assertEqual(migrate(self.repo, self.service.storage), 0)
        self.service.delete(row["id"])

    def test_model_isolation(self):
        doc = DocumentRequest(source_key="integration/model", title="A", text="Text")
        result = self.service.ingest(doc)
        with self.repo.connection() as conn:
            conn.execute("UPDATE rag_service.documents SET embedding_model='different-model' WHERE id=%s",
                         (result["id"],))
        self.assertFalse(self.service.search(SearchRequest(query="Text"))["success"])
        self.repo.delete(result["id"])

    def test_http_to_real_database(self):
        headers = {"Authorization": "Bearer " + "a" * 32}
        with TestClient(create_app(self.settings, self.service)) as client:
            response = client.put("/api/v1/documents", headers=headers,
                                  json={"source_key": "integration/http", "title": "API", "text": "Hello database"})
            self.assertEqual(response.status_code, 200)
            document_id = response.json()["id"]
            response = client.post("/api/v1/knowledge/search", headers=headers, json={"query": "Hello"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["success"])
            self.assertEqual(client.delete(f"/api/v1/documents/{document_id}", headers=headers).status_code, 200)


if __name__ == "__main__":
    unittest.main()
