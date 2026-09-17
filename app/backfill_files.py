"""Export existing source_text to local TXT files without calling Gemini."""
import argparse

from app.config import Settings
from app.repository import Repository
from app.storage import LocalFileStore


def backfill(repository, storage):
    count = 0
    # One row/transaction at a time. Locks prevent an upload/delete from changing
    # a document between reading its text and attaching the exported file.
    while True:
        with repository.connection() as conn:
            row = conn.execute("""
                SELECT id, title, source_text FROM rag_service.documents
                WHERE file_storage_key IS NULL ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
            """).fetchone()
            if row is None:
                return count
            stored = storage.save(row["source_text"].encode("utf-8"), row["title"] + ".txt", origin="recovered_text")
            conn.execute("""
                UPDATE rag_service.documents SET file_storage_key=%s, file_name=%s,
                    file_mime_type=%s, file_size=%s, file_origin=%s WHERE id=%s
            """, (stored.key, stored.name, stored.mime_type, stored.size, stored.origin, row["id"]))
        count += 1


def main():
    parser = argparse.ArgumentParser(description="Xuất văn bản tài liệu cũ thành TXT trong knowlegde")
    parser.add_argument("--confirm", action="store_true")
    if not parser.parse_args().confirm:
        parser.error("Kiểm tra DATABASE_URL rồi thêm --confirm")
    count = backfill(Repository(Settings.load()), LocalFileStore())
    print(f"Exported {count} existing documents as TXT. No embedding calls.")


if __name__ == "__main__":
    main()
