"""PostgreSQL metadata for private images extracted from PDF documents."""
from contextlib import contextmanager
import json

import psycopg
from psycopg.rows import dict_row


class DocumentAssetRepository:
    def __init__(self, settings):
        if settings.database_url is None:
            raise RuntimeError("Thieu DATABASE_URL cho document assets")
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
                CREATE TABLE IF NOT EXISTS rag_metadata.document_assets (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    document_id BIGINT NOT NULL,
                    asset_type VARCHAR(30) NOT NULL DEFAULT 'image',
                    page_number INTEGER NOT NULL CHECK (page_number > 0),
                    section_path JSONB NOT NULL DEFAULT '[]'::jsonb,
                    bbox JSONB NOT NULL,
                    width INTEGER NOT NULL CHECK (width > 0),
                    height INTEGER NOT NULL CHECK (height > 0),
                    media_type VARCHAR(100) NOT NULL,
                    storage_name VARCHAR(255) NOT NULL,
                    checksum VARCHAR(128) NOT NULL,
                    caption TEXT,
                    ocr_text TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(document_id, storage_name)
                )
                """
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_document_assets_document
                   ON rag_metadata.document_assets(document_id, page_number, id)"""
            )

    def replace(self, document_id, assets):
        document_id = int(document_id)
        with self.connection() as conn:
            conn.execute(
                "DELETE FROM rag_metadata.document_assets WHERE document_id=%s",
                (document_id,),
            )
            rows = []
            for asset in assets:
                rows.append(conn.execute(
                    """
                    INSERT INTO rag_metadata.document_assets
                        (document_id, asset_type, page_number, section_path, bbox,
                         width, height, media_type, storage_name, checksum)
                    VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        document_id,
                        asset["asset_type"],
                        asset["page_number"],
                        json.dumps(asset["section_path"], ensure_ascii=False),
                        json.dumps(asset["bbox"]),
                        asset["width"],
                        asset["height"],
                        asset["media_type"],
                        asset["storage_name"],
                        asset["checksum"],
                    ),
                ).fetchone())
        return rows

    def list_for_documents(self, document_ids):
        ids = sorted({int(value) for value in document_ids if value is not None})
        if not ids:
            return []
        with self.connection() as conn:
            return conn.execute(
                """SELECT * FROM rag_metadata.document_assets
                   WHERE document_id = ANY(%s)
                   ORDER BY document_id, page_number, id""",
                (ids,),
            ).fetchall()

    def get(self, asset_id):
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM rag_metadata.document_assets WHERE id=%s",
                (int(asset_id),),
            ).fetchone()

    def delete_document(self, document_id):
        with self.connection() as conn:
            return conn.execute(
                "DELETE FROM rag_metadata.document_assets WHERE document_id=%s RETURNING id",
                (int(document_id),),
            ).fetchall()
