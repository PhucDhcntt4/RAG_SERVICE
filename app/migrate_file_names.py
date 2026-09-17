"""Copy legacy UUID files to knowlegde using their stored display names."""
import argparse

from app.config import Settings
from app.repository import Repository
from app.storage import LocalFileStore


def migrate(repository, storage):
    count = 0
    while True:
        with repository.connection() as conn:
            row = conn.execute("""
                SELECT id, file_storage_key, file_name, file_origin FROM rag_service.documents
                WHERE file_storage_key ~ '^[0-9a-f]{32}[.](txt|md|pdf)$'
                ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
            """).fetchone()
            if row is None:
                return count
            old_key = row["file_storage_key"]
            data = storage.read(old_key)
            saved = storage.save(data, row["file_name"] or old_key, origin=row["file_origin"] or "upload")
            conn.execute("""
                UPDATE rag_service.documents SET file_storage_key=%s, file_name=%s WHERE id=%s
            """, (saved.key, saved.name, row["id"]))
        # Only remove the old file after its replacement reference has committed.
        if not storage.remove(old_key):
            print(f"Document {row['id']}: copied successfully; old file requires manual cleanup.")
        count += 1


def main():
    parser = argparse.ArgumentParser(description="Chuyển file UUID cũ sang knowlegde với tên đã lưu")
    parser.add_argument("--confirm", action="store_true")
    if not parser.parse_args().confirm:
        parser.error("Kiểm tra DATABASE_URL rồi thêm --confirm")
    count = migrate(Repository(Settings.load()), LocalFileStore())
    print(f"Migrated {count} local files to knowlegde. No embedding calls.")


if __name__ == "__main__":
    main()
