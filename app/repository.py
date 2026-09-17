from contextlib import contextmanager

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from app.config import Settings


class Repository:
    def __init__(self, settings: Settings):
        self.dsn = settings.database_url.get_secret_value()

    @contextmanager
    def connection(self):
        with psycopg.connect(self.dsn, connect_timeout=5, row_factory=dict_row,
                             options="-c statement_timeout=15000 -c lock_timeout=5000") as conn:
            register_vector(conn)
            yield conn

    def ready(self):
        with self.connection() as conn:
            conn.execute("SELECT id, file_storage_key FROM rag_service.documents LIMIT 0")
            conn.execute("SELECT embedding FROM rag_service.chunks LIMIT 0")

    def replace(self, document, chunks, vectors, embedder, stored_file=None):
        import hashlib

        if len(chunks) != len(vectors):
            raise ValueError("Chunk/vector count mismatch")
        with self.connection() as conn:
            row = conn.execute("""
                INSERT INTO rag_service.documents
                    (source_key, title, category, source_text, source_checksum,
                     embedding_provider, embedding_model, embedding_dimension)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_key) DO UPDATE SET
                    title=EXCLUDED.title, category=EXCLUDED.category,
                    source_text=EXCLUDED.source_text, source_checksum=EXCLUDED.source_checksum,
                    embedding_provider=EXCLUDED.embedding_provider,
                    embedding_model=EXCLUDED.embedding_model,
                    embedding_dimension=EXCLUDED.embedding_dimension,
                    updated_at=NOW()
                RETURNING id, source_key, title, category, is_active, updated_at, file_storage_key
            """, (document.source_key, document.title, document.category, document.text,
                  hashlib.sha256(document.text.encode()).hexdigest(),
                  embedder.provider, embedder.model, embedder.dimension)).fetchone()
            old_file_key = row.pop("file_storage_key", None)
            # Upsert locks the document row; replacement is atomic even for the same source_key.
            conn.execute("DELETE FROM rag_service.chunks WHERE document_id=%s", (row["id"],))
            with conn.cursor() as cursor:
                cursor.executemany("""
                    INSERT INTO rag_service.chunks(document_id, chunk_index, heading, content, embedding)
                    VALUES (%s, %s, %s, %s, %s)
                """, [(row["id"], c.index, c.heading, c.content, Vector(v))
                      for c, v in zip(chunks, vectors, strict=True)])
            # The upsert holds the row lock until both chunks and file metadata commit.
            conn.execute("""
                UPDATE rag_service.documents SET file_storage_key=%s, file_name=%s,
                    file_mime_type=%s, file_size=%s, file_origin=%s WHERE id=%s
            """, (stored_file.key if stored_file else None, stored_file.name if stored_file else None,
                  stored_file.mime_type if stored_file else None, stored_file.size if stored_file else None,
                  stored_file.origin if stored_file else None, row["id"]))
        return {**row, "chunk_count": len(chunks), "file_name": stored_file.name if stored_file else None,
                "_old_file_key": old_file_key}

    def search(self, vector, embedder, categories, threshold, limit):
        with self.connection() as conn:
            return conn.execute("""
                SELECT d.source_key, d.title, d.category, c.heading, c.chunk_index, c.content,
                       1 - (c.embedding <=> %s) AS similarity
                FROM rag_service.chunks c JOIN rag_service.documents d ON d.id=c.document_id
                WHERE d.is_active AND d.embedding_provider=%s AND d.embedding_model=%s
                  AND d.embedding_dimension=%s
                  AND (%s::text[] IS NULL OR d.category = ANY(%s::text[]))
                  AND 1 - (c.embedding <=> %s) >= %s
                ORDER BY similarity DESC, d.id, c.chunk_index LIMIT %s
            """, (Vector(vector), embedder.provider, embedder.model, embedder.dimension,
                  categories or None, categories or None, Vector(vector), threshold, limit)).fetchall()

    def list_documents(self, limit, offset):
        with self.connection() as conn:
            return conn.execute("""
                SELECT d.id, d.source_key, d.title, d.category, d.is_active,
                       d.embedding_provider, d.embedding_model, d.embedding_dimension,
                       d.created_at, d.updated_at, d.file_name, d.file_size, d.file_origin,
                       (d.file_storage_key IS NOT NULL) AS has_local_file, COUNT(c.id) AS chunk_count
                FROM rag_service.documents d LEFT JOIN rag_service.chunks c ON c.document_id=d.id
                GROUP BY d.id ORDER BY d.updated_at DESC, d.id DESC LIMIT %s OFFSET %s
            """, (limit, offset)).fetchall()

    def get_document(self, document_id):
        with self.connection() as conn:
            return conn.execute("SELECT * FROM rag_service.documents WHERE id=%s", (document_id,)).fetchone()

    def set_active(self, document_id, active):
        with self.connection() as conn:
            return conn.execute("""
                UPDATE rag_service.documents SET is_active=%s, updated_at=NOW()
                WHERE id=%s RETURNING id, is_active
            """, (active, document_id)).fetchone()

    def delete(self, document_id):
        with self.connection() as conn:
            return conn.execute("DELETE FROM rag_service.documents WHERE id=%s RETURNING id, file_storage_key",
                                (document_id,)).fetchone()
