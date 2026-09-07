import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.embeddings import EmbeddingError
from app.models import DocumentRequest
from app.storage import LocalFileStore, StorageError
from test_service import make_service


class LocalStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalFileStore(Path(self.temp.name) / "files")
        self.svc = make_service()
        self.svc.storage = self.store
        self.document = DocumentRequest(source_key="../../store", title="Store", text="Store information")

    def test_original_bytes_and_filename_collision(self):
        raw = b"\xef\xbb\xbfStore\r\nOpening hours\r\n"
        first = self.store.save(raw, "../store.txt")
        second = self.store.save(b"other", "store.txt")
        self.assertNotEqual(first.key, second.key)
        self.assertEqual(first.name, "store.txt")
        self.assertEqual(self.store.path(first.key).name, "store.txt")
        self.assertEqual(self.store.path(second.key).name, "store (2).txt")
        self.assertEqual(self.store.read(first.key), raw)
        self.assertEqual(self.store.read(second.key), b"other")
        self.assertEqual(self.store.path(first.key).parent, self.store.root)

    def test_rejects_paths_outside_storage(self):
        for key in ("../.env", "C:\\Windows\\a.txt", "../" + "a" * 32 + ".txt", "a" * 32 + ".exe",
                    "named/../store.txt", "named/C:\\store.txt", "named/folder/store.txt", "named/test.txt:secret"):
            with self.subTest(key=key), self.assertRaises(StorageError):
                self.store.read(key)

    def test_keeps_unicode_filename_and_extension_case(self):
        stored = self.store.save(b"file bytes", "Cửa hàng Đông Hải.TXT")
        self.assertEqual(self.store.path(stored.key).name, "Cửa hàng Đông Hải.TXT")
        self.assertEqual(stored.mime_type, "text/plain")

    def test_simultaneous_same_filename_uploads_do_not_overwrite(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            files = list(pool.map(lambda n: self.store.save(str(n).encode(), "store.txt"), range(5)))
        self.assertEqual(len({file.key for file in files}), 5)
        for n, file in enumerate(files):
            self.assertEqual(self.store.read(file.key), str(n).encode())

    def test_legacy_files_remain_readable_from_previous_directory(self):
        old_root = Path(self.temp.name) / "legacy"
        old_root.mkdir()
        key = "a" * 32 + ".txt"
        (old_root / key).write_bytes(b"legacy")
        store = LocalFileStore(self.store.root, legacy_root=old_root)
        self.assertEqual(store.read(key), b"legacy")
        current = store.save(b"current", key)
        self.assertEqual(store.read(current.key), b"current")
        self.assertEqual(store.read(key), b"legacy")

    def test_disk_write_failure_never_updates_database(self):
        with patch.object(self.store, "save", side_effect=StorageError("disk full")):
            with self.assertRaises(StorageError):
                self.svc.ingest(self.document)
        self.svc.repository.replace.assert_not_called()

    def test_failed_embedding_never_writes_file(self):
        self.svc.embedder.embed.side_effect = EmbeddingError("unavailable")
        with self.assertRaises(EmbeddingError):
            self.svc.ingest(self.document)
        self.assertFalse(self.store.root.exists())

    def test_successful_replacement_removes_only_previous_file(self):
        old = self.store.save(b"old", "store.txt")
        unrelated = self.store.save(b"unrelated", "store.txt")
        self.svc.repository.replace.return_value = {"id": 1, "_old_file_key": old.key}
        result = self.svc.ingest(self.document, original_data=b"new", original_filename="store.txt")
        new = self.svc.repository.replace.call_args.kwargs["stored_file"]
        self.assertEqual(self.store.read(new.key), b"new")
        self.assertFalse(self.store.path(old.key).exists())
        self.assertEqual(self.store.read(unrelated.key), b"unrelated")
        self.assertNotIn("_old_file_key", result)

    def test_uncertain_commit_retains_new_and_existing_files(self):
        old = self.store.save(b"old", "store.txt")
        self.svc.repository.replace.side_effect = RuntimeError("connection lost during commit")
        with self.assertRaises(RuntimeError):
            self.svc.ingest(self.document)
        new = self.svc.repository.replace.call_args.kwargs["stored_file"]
        self.assertEqual(self.store.read(old.key), b"old")
        self.assertEqual(self.store.read(new.key), self.document.text.encode())

    def test_delete_waits_for_database_success(self):
        file = self.store.save(b"original", "store.txt")
        self.svc.repository.delete.side_effect = RuntimeError("DB failure")
        with self.assertRaises(RuntimeError):
            self.svc.delete(1)
        self.assertTrue(self.store.path(file.key).exists())
        self.svc.repository.delete.side_effect = None
        self.svc.repository.delete.return_value = {"id": 1, "file_storage_key": file.key}
        self.assertTrue(self.svc.delete(1)["local_file_deleted"])
        self.assertFalse(self.store.path(file.key).exists())


if __name__ == "__main__":
    unittest.main()
