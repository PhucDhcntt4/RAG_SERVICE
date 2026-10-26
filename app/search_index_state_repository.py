from __future__ import annotations

from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


KNOWLEDGE_BM25_INDEX = "knowledge_bm25"


class SearchIndexStateRepository:
    """Shared revision used to invalidate process-local search indexes."""

    def __init__(self, settings):
        if settings.database_url is None:
            raise RuntimeError("Thiếu DATABASE_URL cho search index state")
        self.dsn = settings.database_url.get_secret_value()

    @contextmanager
    def connection(self):
        with psycopg.connect(
            self.dsn,
            connect_timeout=5,
            row_factory=dict_row,
            options="-c statement_timeout=15000 -c lock_timeout=5000",
        ) as conn:
            yield conn

    def initialize(self):
        with self.connection() as conn:
            conn.execute("CREATE SCHEMA IF NOT EXISTS rag_metadata")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_metadata.search_index_state (
                    index_name VARCHAR(100) PRIMARY KEY,
                    revision BIGINT NOT NULL DEFAULT 0
                        CHECK (revision >= 0),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                """
                INSERT INTO rag_metadata.search_index_state (
                    index_name,
                    revision
                )
                VALUES (%s, 0)
                ON CONFLICT (index_name) DO NOTHING
                """,
                (KNOWLEDGE_BM25_INDEX,),
            )

    def current_revision(self, index_name: str = KNOWLEDGE_BM25_INDEX) -> int:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT revision
                FROM rag_metadata.search_index_state
                WHERE index_name = %s
                """,
                (index_name,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"Chưa khởi tạo search index state: {index_name}")
        return int(row["revision"])

    def bump_revision(self, index_name: str = KNOWLEDGE_BM25_INDEX) -> int:
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE rag_metadata.search_index_state
                SET revision = revision + 1,
                    updated_at = NOW()
                WHERE index_name = %s
                RETURNING revision
                """,
                (index_name,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"Chưa khởi tạo search index state: {index_name}")
        return int(row["revision"])
