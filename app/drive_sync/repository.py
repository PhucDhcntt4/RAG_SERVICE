from __future__ import annotations

from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


DRIVE_WORKER_LOCK_KEY_1 = 0x44485247
DRIVE_WORKER_LOCK_KEY_2 = 0x44525645
MAX_JOB_ATTEMPTS = 3


class GoogleDriveSyncRepository:
    """Quản lý cấu hình, job và tài liệu đồng bộ từ Google Drive."""

    def __init__(self, settings):
        if settings.database_url is None:
            raise RuntimeError("Thiếu DATABASE_URL cho Google Drive Sync")

        self.dsn = settings.database_url.get_secret_value()
        self.default_poll_minutes = max(
            1,
            (int(settings.google_drive_poll_seconds) + 59) // 60,
        )

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

            self._create_sources_table(conn)
            self._create_settings_table(conn)
            self._create_jobs_table(conn)
            self._create_documents_table(conn)
            self._create_worker_state_table(conn)
            self._create_indexes(conn)

    @staticmethod
    def _create_sources_table(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_metadata.google_drive_sources (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

                folder_id VARCHAR(255) NOT NULL UNIQUE,
                folder_name VARCHAR(255) NOT NULL DEFAULT '',

                department_code VARCHAR(100) NOT NULL DEFAULT '',
                department_name VARCHAR(200) NOT NULL DEFAULT '',

                doc_type_id BIGINT NOT NULL
                    REFERENCES rag_metadata.doc_types(id)
                    ON DELETE RESTRICT,

                enabled BOOLEAN NOT NULL DEFAULT TRUE,

                poll_seconds INTEGER NOT NULL DEFAULT 60
                    CHECK (poll_seconds BETWEEN 10 AND 86400),

                last_scan_at TIMESTAMPTZ,
                last_success_at TIMESTAMPTZ,
                last_error TEXT NOT NULL DEFAULT '',

                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

    def _create_settings_table(self, conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_metadata.google_drive_sync_settings (
                id SMALLINT PRIMARY KEY CHECK (id = 1),
                poll_minutes INTEGER NOT NULL
                    CHECK (poll_minutes BETWEEN 1 AND 1440),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            INSERT INTO rag_metadata.google_drive_sync_settings (
                id,
                poll_minutes
            )
            VALUES (
                1,
                COALESCE(
                    (
                        SELECT GREATEST(1, CEIL(poll_seconds / 60.0)::INTEGER)
                        FROM rag_metadata.google_drive_sources
                        ORDER BY id
                        LIMIT 1
                    ),
                    %s
                )
            )
            ON CONFLICT (id) DO NOTHING
            """,
            (self.default_poll_minutes,),
        )

    @staticmethod
    def _create_jobs_table(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_metadata.google_drive_sync_jobs (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

                source_id BIGINT NOT NULL
                    REFERENCES rag_metadata.google_drive_sources(id)
                    ON DELETE CASCADE,

                trigger_type VARCHAR(20) NOT NULL
                    CHECK (trigger_type IN ('manual', 'scheduled')),

                status VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (
                        status IN (
                            'pending',
                            'running',
                            'completed',
                            'partial',
                            'failed'
                        )
                    ),

                worker_instance VARCHAR(100) NOT NULL DEFAULT '',

                discovered_files INTEGER NOT NULL DEFAULT 0,
                processed_files INTEGER NOT NULL DEFAULT 0,
                imported_files INTEGER NOT NULL DEFAULT 0,
                skipped_files INTEGER NOT NULL DEFAULT 0,
                failed_files INTEGER NOT NULL DEFAULT 0,

                message TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',

                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                heartbeat_at TIMESTAMPTZ,
                attempt_count INTEGER NOT NULL DEFAULT 0
                    CHECK (attempt_count >= 0),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            ALTER TABLE rag_metadata.google_drive_sync_jobs
            ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ
            """
        )
        conn.execute(
            """
            ALTER TABLE rag_metadata.google_drive_sync_jobs
            ADD COLUMN IF NOT EXISTS attempt_count INTEGER
                NOT NULL DEFAULT 0
                CHECK (attempt_count >= 0)
            """
        )

    @staticmethod
    def _create_documents_table(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_metadata.google_drive_documents (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

                source_id BIGINT NOT NULL
                    REFERENCES rag_metadata.google_drive_sources(id)
                    ON DELETE CASCADE,

                last_job_id BIGINT
                    REFERENCES rag_metadata.google_drive_sync_jobs(id)
                    ON DELETE SET NULL,

                group_id BIGINT
                    REFERENCES rag_metadata.document_groups(id)
                    ON DELETE RESTRICT,

                drive_file_id VARCHAR(255) NOT NULL UNIQUE,

                drive_scope VARCHAR(20) NOT NULL
                    CHECK (drive_scope IN ('public', 'private')),

                drive_stage VARCHAR(20) NOT NULL DEFAULT 'NEW'
                    CHECK (drive_stage IN ('NEW', 'DONE', 'ERROR')),

                document_code VARCHAR(100) NOT NULL DEFAULT '',
                document_kind_code VARCHAR(20) NOT NULL DEFAULT '',
                sequence_number INTEGER,
                document_slug VARCHAR(300) NOT NULL DEFAULT '',
                document_version INTEGER
                    CHECK (
                        document_version IS NULL
                        OR document_version > 0
                    ),

                original_file_name VARCHAR(500) NOT NULL,
                file_extension VARCHAR(30) NOT NULL DEFAULT '',
                mime_type VARCHAR(200) NOT NULL DEFAULT '',

                drive_md5_checksum VARCHAR(128),
                content_sha256 VARCHAR(64),

                drive_created_at TIMESTAMPTZ,
                drive_modified_at TIMESTAMPTZ,

                source_key VARCHAR(500) NOT NULL UNIQUE,
                qdrant_document_id BIGINT UNIQUE,

                sync_action VARCHAR(30) NOT NULL DEFAULT ''
                    CHECK (
                        sync_action IN (
                            '',
                            'CREATE',
                            'NEW_VERSION',
                            'DUPLICATE'
                        )
                    ),

                status VARCHAR(30) NOT NULL DEFAULT 'discovered'
                    CHECK (
                        status IN (
                            'discovered',
                            'processing',
                            'completed',
                            'skipped',
                            'error'
                        )
                    ),

                is_active BOOLEAN NOT NULL DEFAULT FALSE,

                error_code VARCHAR(100) NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                move_error TEXT NOT NULL DEFAULT '',

                processed_at TIMESTAMPTZ,
                moved_at TIMESTAMPTZ,

                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                CHECK (
                    is_active = FALSE
                    OR status = 'completed'
                )
            )
            """
        )

        # Additive migration for databases initialized before version planning.
        conn.execute(
            """
            ALTER TABLE rag_metadata.google_drive_documents
            ADD COLUMN IF NOT EXISTS sync_action VARCHAR(30)
                NOT NULL DEFAULT ''
                CHECK (
                    sync_action IN (
                        '',
                        'CREATE',
                        'NEW_VERSION',
                        'DUPLICATE'
                    )
                )
            """
        )

    @staticmethod
    def _create_worker_state_table(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_metadata.google_drive_sync_worker_state (
                id SMALLINT PRIMARY KEY CHECK (id = 1),
                instance_id VARCHAR(100) NOT NULL DEFAULT '',
                current_job_id BIGINT
                    REFERENCES rag_metadata.google_drive_sync_jobs(id)
                    ON DELETE SET NULL,
                started_at TIMESTAMPTZ,
                heartbeat_at TIMESTAMPTZ,
                stopped_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

    @staticmethod
    def _create_indexes(conn):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_google_drive_sources_enabled
            ON rag_metadata.google_drive_sources(enabled, id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_google_drive_jobs_queue
            ON rag_metadata.google_drive_sync_jobs(
                status,
                created_at,
                id
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_google_drive_jobs_source
            ON rag_metadata.google_drive_sync_jobs(
                source_id,
                created_at DESC
            )
            """
        )

        # Older databases may contain duplicate queued rows from manual tests.
        # Keep the oldest active job and close the rest before adding the guard.
        conn.execute(
            """
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY source_id
                           ORDER BY created_at, id
                       ) AS row_number
                FROM rag_metadata.google_drive_sync_jobs
                WHERE status IN ('pending', 'running')
            )
            UPDATE rag_metadata.google_drive_sync_jobs AS job
            SET status = 'failed',
                error_message = 'Job trùng được đóng khi khởi tạo queue.',
                finished_at = NOW(),
                updated_at = NOW()
            FROM ranked
            WHERE job.id = ranked.id
              AND ranked.row_number > 1
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                uq_google_drive_jobs_active_source
            ON rag_metadata.google_drive_sync_jobs(source_id)
            WHERE status IN ('pending', 'running')
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_google_drive_documents_status
            ON rag_metadata.google_drive_documents(
                status,
                drive_stage,
                updated_at
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_google_drive_documents_code_version
            ON rag_metadata.google_drive_documents(
                source_id,
                document_code,
                document_version DESC
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_google_drive_documents_group
            ON rag_metadata.google_drive_documents(
                group_id,
                is_active
            )
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                uq_google_drive_active_document
            ON rag_metadata.google_drive_documents(
                source_id,
                document_code
            )
            WHERE is_active = TRUE
            """
        )

    def ready(self):
        with self.connection() as conn:
            conn.execute(
                """
                SELECT id
                FROM rag_metadata.google_drive_sources
                LIMIT 0
                """
            )

            conn.execute(
                """
                SELECT id
                FROM rag_metadata.google_drive_sync_settings
                LIMIT 0
                """
            )

            conn.execute(
                """
                SELECT id
                FROM rag_metadata.google_drive_sync_worker_state
                LIMIT 0
                """
            )

            conn.execute(
                """
                SELECT id
                FROM rag_metadata.google_drive_sync_jobs
                LIMIT 0
                """
            )

            conn.execute(
                """
                SELECT id
                FROM rag_metadata.google_drive_documents
                LIMIT 0
                """
            )

    def get_source_by_folder_id(self, folder_id: str):
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM rag_metadata.google_drive_sources
                WHERE folder_id = %s
                """,
                ((folder_id or "").strip(),),
            ).fetchone()

    def get_source(self, source_id: int):
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM rag_metadata.google_drive_sources
                WHERE id = %s
                """,
                (source_id,),
            ).fetchone()

    def upsert_source(
        self,
        *,
        folder_id: str,
        folder_name: str,
        department_code: str,
        department_name: str,
        doc_type_id: int,
    ):
        with self.connection() as conn:
            return conn.execute(
                """
                INSERT INTO rag_metadata.google_drive_sources (
                    folder_id,
                    folder_name,
                    department_code,
                    department_name,
                    doc_type_id
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (folder_id)
                DO UPDATE SET
                    folder_name = EXCLUDED.folder_name,
                    department_code = EXCLUDED.department_code,
                    department_name = EXCLUDED.department_name,
                    doc_type_id = EXCLUDED.doc_type_id,
                    updated_at = NOW()
                RETURNING *
                """,
                (
                    (folder_id or "").strip(),
                    (folder_name or "").strip(),
                    (department_code or "").strip().upper(),
                    (department_name or "").strip(),
                    doc_type_id,
                ),
            ).fetchone()

    def list_sources(self, *, include_disabled: bool = True):
        where = "" if include_disabled else "WHERE enabled = TRUE"
        with self.connection() as conn:
            return conn.execute(
                f"""
                SELECT *
                FROM rag_metadata.google_drive_sources
                {where}
                ORDER BY department_name, id
                """
            ).fetchall()

    def dashboard_sources(self):
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT source.*,
                       COALESCE(stats.total_documents, 0) AS total_documents,
                       COALESCE(stats.active_documents, 0) AS active_documents,
                       COALESCE(stats.error_documents, 0) AS error_documents,
                       latest.id AS latest_job_id,
                       latest.status AS latest_job_status,
                       latest.trigger_type AS latest_job_trigger,
                       latest.created_at AS latest_job_created_at,
                       latest.finished_at AS latest_job_finished_at
                FROM rag_metadata.google_drive_sources AS source
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS total_documents,
                           COUNT(*) FILTER (WHERE document.is_active = TRUE)
                               AS active_documents,
                           COUNT(*) FILTER (WHERE document.status = 'error')
                               AS error_documents
                    FROM rag_metadata.google_drive_documents AS document
                    WHERE document.source_id = source.id
                ) AS stats ON TRUE
                LEFT JOIN LATERAL (
                    SELECT job.id,
                           job.status,
                           job.trigger_type,
                           job.created_at,
                           job.finished_at
                    FROM rag_metadata.google_drive_sync_jobs AS job
                    WHERE job.source_id = source.id
                    ORDER BY job.created_at DESC, job.id DESC
                    LIMIT 1
                ) AS latest ON TRUE
                ORDER BY source.department_name, source.id
                """
            ).fetchall()

    def update_source_settings(
        self,
        source_id: int,
        *,
        enabled: bool,
    ):
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.google_drive_sources
                SET enabled = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (enabled, source_id),
            ).fetchone()

    def get_sync_settings(self):
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT poll_minutes, updated_at
                FROM rag_metadata.google_drive_sync_settings
                WHERE id = 1
                """
            ).fetchone()

    def update_sync_settings(self, *, poll_minutes: int):
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.google_drive_sync_settings
                SET poll_minutes = %s,
                    updated_at = NOW()
                WHERE id = 1
                RETURNING poll_minutes, updated_at
                """,
                (poll_minutes,),
            ).fetchone()

    def jobs_page(
        self,
        *,
        page: int = 1,
        page_size: int = 10,
        source_id: int | None = None,
        status: str | None = None,
    ):
        clauses = []
        values = []
        if source_id is not None:
            clauses.append("job.source_id = %s")
            values.append(source_id)
        if status:
            clauses.append("job.status = %s")
            values.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        offset = (page - 1) * page_size

        with self.connection() as conn:
            total = conn.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM rag_metadata.google_drive_sync_jobs AS job
                {where}
                """,
                values,
            ).fetchone()["total"]
            items = conn.execute(
                f"""
                SELECT job.*, source.department_code, source.department_name
                FROM rag_metadata.google_drive_sync_jobs AS job
                JOIN rag_metadata.google_drive_sources AS source
                  ON source.id = job.source_id
                {where}
                ORDER BY job.created_at DESC, job.id DESC
                LIMIT %s OFFSET %s
                """,
                [*values, page_size, offset],
            ).fetchall()

        total_pages = max(1, (int(total) + page_size - 1) // page_size)
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": int(total),
            "total_pages": total_pages,
        }

    def get_job(self, job_id: int):
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT job.*, source.department_code, source.department_name
                FROM rag_metadata.google_drive_sync_jobs AS job
                JOIN rag_metadata.google_drive_sources AS source
                  ON source.id = job.source_id
                WHERE job.id = %s
                """,
                (job_id,),
            ).fetchone()

    def job_documents_page(
        self,
        job_id: int,
        *,
        page: int = 1,
        page_size: int = 10,
    ):
        offset = (page - 1) * page_size
        with self.connection() as conn:
            total = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM rag_metadata.google_drive_documents
                WHERE last_job_id = %s
                """,
                (job_id,),
            ).fetchone()["total"]
            items = conn.execute(
                """
                SELECT id,
                       drive_file_id,
                       drive_scope,
                       drive_stage,
                       document_code,
                       document_version,
                       original_file_name,
                       sync_action,
                       status,
                       is_active,
                       error_code,
                       error_message,
                       move_error,
                       processed_at,
                       moved_at,
                       updated_at
                FROM rag_metadata.google_drive_documents
                WHERE last_job_id = %s
                ORDER BY updated_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                (job_id, page_size, offset),
            ).fetchall()

        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": int(total),
            "total_pages": max(1, (int(total) + page_size - 1) // page_size),
        }

    def document_metadata_by_qdrant_ids(self, document_ids):
        normalized = []
        for value in document_ids:
            try:
                normalized.append(int(value))
            except (TypeError, ValueError):
                continue
        if not normalized:
            return {}

        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT qdrant_document_id,
                       document_code,
                       document_version,
                       drive_scope,
                       is_active
                FROM rag_metadata.google_drive_documents
                WHERE qdrant_document_id = ANY(%s)
                """,
                (normalized,),
            ).fetchall()
        return {
            str(row["qdrant_document_id"]): row
            for row in rows
            if row["qdrant_document_id"] is not None
        }

    def heartbeat_worker(
        self,
        instance_id: str,
        *,
        current_job_id: int | None = None,
    ):
        with self.connection() as conn:
            return conn.execute(
                """
                INSERT INTO rag_metadata.google_drive_sync_worker_state (
                    id,
                    instance_id,
                    current_job_id,
                    started_at,
                    heartbeat_at,
                    stopped_at,
                    updated_at
                )
                VALUES (1, %s, %s, NOW(), NOW(), NULL, NOW())
                ON CONFLICT (id)
                DO UPDATE SET
                    instance_id = EXCLUDED.instance_id,
                    current_job_id = EXCLUDED.current_job_id,
                    started_at = CASE
                        WHEN rag_metadata.google_drive_sync_worker_state.instance_id
                             <> EXCLUDED.instance_id
                        THEN NOW()
                        ELSE rag_metadata.google_drive_sync_worker_state.started_at
                    END,
                    heartbeat_at = NOW(),
                    stopped_at = NULL,
                    updated_at = NOW()
                RETURNING *
                """,
                (instance_id, current_job_id),
            ).fetchone()

    def stop_worker(self, instance_id: str):
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.google_drive_sync_worker_state
                SET current_job_id = NULL,
                    stopped_at = NOW(),
                    updated_at = NOW()
                WHERE id = 1
                  AND instance_id = %s
                RETURNING *
                """,
                (instance_id,),
            ).fetchone()

    def worker_status(self, *, online_within_seconds: int = 150):
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT state.*,
                       state.stopped_at IS NULL
                       AND state.heartbeat_at >= NOW() - make_interval(secs => %s)
                           AS online
                FROM rag_metadata.google_drive_sync_worker_state AS state
                WHERE state.id = 1
                """,
                (online_within_seconds,),
            ).fetchone()
        if row is not None:
            return row
        return {
            "instance_id": "",
            "current_job_id": None,
            "started_at": None,
            "heartbeat_at": None,
            "stopped_at": None,
            "online": False,
        }

    def record_scanned_document(
        self,
        *,
        source_id: int,
        group_id: int,
        drive_scope: str,
        drive_item: dict,
        job_id: int | None = None,
        parsed=None,
        error_code: str = "",
        error_message: str = "",
    ):
        file_id = str(drive_item.get("id") or "").strip()
        file_name = str(drive_item.get("name") or "").strip()
        if not file_id or not file_name:
            raise ValueError("Google Drive item thiếu id hoặc name")

        parsed_data = parsed.as_dict() if parsed is not None else {}
        status = "error" if error_code else "discovered"

        with self.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO rag_metadata.google_drive_documents (
                    source_id,
                    last_job_id,
                    group_id,
                    drive_file_id,
                    drive_scope,
                    drive_stage,
                    document_code,
                    document_kind_code,
                    sequence_number,
                    document_slug,
                    document_version,
                    original_file_name,
                    file_extension,
                    mime_type,
                    drive_md5_checksum,
                    drive_created_at,
                    drive_modified_at,
                    source_key,
                    status,
                    is_active,
                    error_code,
                    error_message,
                    updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, 'NEW',
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, FALSE, %s, %s, NOW()
                )
                ON CONFLICT (drive_file_id)
                DO UPDATE SET
                    source_id = EXCLUDED.source_id,
                    last_job_id = EXCLUDED.last_job_id,
                    group_id = EXCLUDED.group_id,
                    drive_scope = EXCLUDED.drive_scope,
                    drive_stage = 'NEW',
                    document_code = EXCLUDED.document_code,
                    document_kind_code = EXCLUDED.document_kind_code,
                    sequence_number = EXCLUDED.sequence_number,
                    document_slug = EXCLUDED.document_slug,
                    document_version = EXCLUDED.document_version,
                    original_file_name = EXCLUDED.original_file_name,
                    file_extension = EXCLUDED.file_extension,
                    mime_type = EXCLUDED.mime_type,
                    drive_md5_checksum = EXCLUDED.drive_md5_checksum,
                    drive_created_at = EXCLUDED.drive_created_at,
                    drive_modified_at = EXCLUDED.drive_modified_at,
                    status = EXCLUDED.status,
                    error_code = EXCLUDED.error_code,
                    error_message = EXCLUDED.error_message,
                    updated_at = NOW()
                WHERE rag_metadata.google_drive_documents.status
                      NOT IN ('processing', 'completed', 'skipped')
                RETURNING *
                """,
                (
                    source_id,
                    job_id,
                    group_id,
                    file_id,
                    drive_scope,
                    parsed_data.get("document_code", ""),
                    parsed_data.get("document_kind_code", ""),
                    parsed_data.get("sequence_number"),
                    parsed_data.get("document_slug", ""),
                    parsed_data.get("document_version"),
                    file_name,
                    parsed_data.get("file_extension", ""),
                    str(drive_item.get("mimeType") or ""),
                    drive_item.get("md5Checksum"),
                    drive_item.get("createdTime"),
                    drive_item.get("modifiedTime"),
                    f"gdrive/{file_id}",
                    status,
                    error_code,
                    error_message,
                ),
            ).fetchone()

            if row is not None:
                return row

            # A completed/processing record is immutable during rediscovery.
            return conn.execute(
                """
                SELECT *
                FROM rag_metadata.google_drive_documents
                WHERE drive_file_id = %s
                """,
                (file_id,),
            ).fetchone()

    def list_documents_for_version_check(self, source_id: int):
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM rag_metadata.google_drive_documents
                WHERE source_id = %s
                  AND drive_stage = 'NEW'
                  AND status = 'discovered'
                ORDER BY created_at, id
                """,
                (source_id,),
            ).fetchall()

    def list_documents_ready_for_import(self, source_id: int):
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM rag_metadata.google_drive_documents
                WHERE source_id = %s
                  AND drive_stage = 'NEW'
                  AND status = 'discovered'
                  AND sync_action IN ('CREATE', 'NEW_VERSION')
                ORDER BY created_at, id
                """,
                (source_id,),
            ).fetchall()

    def list_completed_versions(self, source_id: int, document_code: str):
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM rag_metadata.google_drive_documents
                WHERE source_id = %s
                  AND document_code = %s
                  AND status = 'completed'
                ORDER BY document_version DESC, id DESC
                """,
                (source_id, document_code),
            ).fetchall()

    def save_version_decision(
        self,
        *,
        drive_file_id: str,
        content_sha256: str,
        sync_action: str = "",
        status: str = "discovered",
        error_code: str = "",
        error_message: str = "",
    ):
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE rag_metadata.google_drive_documents
                SET content_sha256 = %s,
                    sync_action = %s,
                    status = %s,
                    error_code = %s,
                    error_message = %s,
                    processed_at = CASE
                        WHEN %s IN ('skipped', 'error') THEN NOW()
                        ELSE NULL
                    END,
                    updated_at = NOW()
                WHERE drive_file_id = %s
                  AND status = 'discovered'
                RETURNING *
                """,
                (
                    content_sha256,
                    sync_action,
                    status,
                    error_code,
                    error_message,
                    status,
                    drive_file_id,
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError(
                "Tài liệu không còn ở trạng thái discovered"
            )
        return row

    def claim_document_for_import(self, document_id: int):
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.google_drive_documents
                SET status = 'processing',
                    error_code = '',
                    error_message = '',
                    updated_at = NOW()
                WHERE id = %s
                  AND drive_stage = 'NEW'
                  AND status = 'discovered'
                  AND sync_action IN ('CREATE', 'NEW_VERSION')
                RETURNING *
                """,
                (document_id,),
            ).fetchone()

    def list_active_versions(
        self,
        source_id: int,
        document_code: str,
        *,
        exclude_id: int | None = None,
    ):
        values = [source_id, document_code]
        exclude = ""
        if exclude_id is not None:
            exclude = "AND id <> %s"
            values.append(exclude_id)

        with self.connection() as conn:
            return conn.execute(
                f"""
                SELECT *
                FROM rag_metadata.google_drive_documents
                WHERE source_id = %s
                  AND document_code = %s
                  AND is_active = TRUE
                  {exclude}
                ORDER BY document_version DESC, id DESC
                """,
                values,
            ).fetchall()

    def save_qdrant_document_id(self, document_id: int, qdrant_document_id: int):
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE rag_metadata.google_drive_documents
                SET qdrant_document_id = %s,
                    updated_at = NOW()
                WHERE id = %s
                  AND status = 'processing'
                RETURNING *
                """,
                (qdrant_document_id, document_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("Tài liệu không còn ở trạng thái processing")
        return row

    def complete_import(self, document_id: int):
        with self.connection() as conn:
            current = conn.execute(
                """
                SELECT *
                FROM rag_metadata.google_drive_documents
                WHERE id = %s
                  AND status = 'processing'
                FOR UPDATE
                """,
                (document_id,),
            ).fetchone()
            if current is None:
                raise RuntimeError("Không tìm thấy tài liệu đang xử lý")
            if current["qdrant_document_id"] is None:
                raise RuntimeError("Tài liệu chưa có Qdrant document ID")

            conn.execute(
                """
                UPDATE rag_metadata.google_drive_documents
                SET is_active = FALSE,
                    updated_at = NOW()
                WHERE source_id = %s
                  AND document_code = %s
                  AND id <> %s
                  AND is_active = TRUE
                """,
                (current["source_id"], current["document_code"], current["id"]),
            )
            return conn.execute(
                """
                UPDATE rag_metadata.google_drive_documents
                SET status = 'completed',
                    is_active = TRUE,
                    error_code = '',
                    error_message = '',
                    processed_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (document_id,),
            ).fetchone()

    def fail_import(
        self,
        document_id: int,
        *,
        error_code: str,
        error_message: str,
    ):
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.google_drive_documents
                SET status = 'error',
                    is_active = FALSE,
                    error_code = %s,
                    error_message = %s,
                    processed_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                  AND status IN ('discovered', 'processing')
                RETURNING *
                """,
                (error_code[:100], error_message, document_id),
            ).fetchone()

    def record_processing_issue(
        self,
        document_id: int,
        *,
        error_code: str,
        error_message: str,
    ):
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.google_drive_documents
                SET error_code = %s,
                    error_message = %s,
                    updated_at = NOW()
                WHERE id = %s
                  AND status = 'processing'
                RETURNING *
                """,
                (error_code[:100], error_message, document_id),
            ).fetchone()

    def list_documents_pending_move(self, source_id: int):
        """Lấy các bản ghi đã xử lý nhưng file vẫn ở Drive/NEW."""
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM rag_metadata.google_drive_documents
                WHERE source_id = %s
                  AND drive_stage = 'NEW'
                  AND status IN ('completed', 'skipped', 'error')
                ORDER BY processed_at NULLS LAST, id
                """,
                (source_id,),
            ).fetchall()

    def mark_document_moved(self, document_id: int, *, drive_stage: str):
        if drive_stage not in {"DONE", "ERROR"}:
            raise ValueError(
                "drive_stage sau khi move chỉ có thể là DONE hoặc ERROR"
            )

        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE rag_metadata.google_drive_documents
                SET drive_stage = %s,
                    move_error = '',
                    moved_at = COALESCE(moved_at, NOW()),
                    updated_at = NOW()
                WHERE id = %s
                  AND drive_stage = 'NEW'
                  AND status IN ('completed', 'skipped', 'error')
                RETURNING *
                """,
                (drive_stage, document_id),
            ).fetchone()

        if row is None:
            raise RuntimeError(
                "Tài liệu không còn sẵn sàng để cập nhật move"
            )
        return row

    def record_move_error(self, document_id: int, error_message: str):
        """Giữ drive_stage=NEW để lần chạy sau có thể thử move lại."""
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.google_drive_documents
                SET move_error = %s,
                    updated_at = NOW()
                WHERE id = %s
                  AND drive_stage = 'NEW'
                  AND status IN ('completed', 'skipped', 'error')
                RETURNING *
                """,
                (error_message, document_id),
            ).fetchone()

    def enqueue_job(self, source_id: int, *, trigger_type: str = "manual"):
        if trigger_type not in {"manual", "scheduled"}:
            raise ValueError("trigger_type không hợp lệ")

        with self.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO rag_metadata.google_drive_sync_jobs (
                    source_id,
                    trigger_type,
                    status
                )
                VALUES (%s, %s, 'pending')
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (source_id, trigger_type),
            ).fetchone()
            if row is not None:
                return row
            return conn.execute(
                """
                SELECT *
                FROM rag_metadata.google_drive_sync_jobs
                WHERE source_id = %s
                  AND status IN ('pending', 'running')
                ORDER BY id
                LIMIT 1
                """,
                (source_id,),
            ).fetchone()

    def enqueue_due_sources(self):
        with self.connection() as conn:
            return conn.execute(
                """
                INSERT INTO rag_metadata.google_drive_sync_jobs (
                    source_id,
                    trigger_type,
                    status
                )
                SELECT source.id, 'scheduled', 'pending'
                FROM rag_metadata.google_drive_sources AS source
                CROSS JOIN rag_metadata.google_drive_sync_settings AS settings
                WHERE source.enabled = TRUE
                  AND (
                        source.last_scan_at IS NULL
                        OR source.last_scan_at
                           + make_interval(mins => settings.poll_minutes)
                           <= NOW()
                  )
                  AND NOT EXISTS (
                        SELECT 1
                        FROM rag_metadata.google_drive_sync_jobs AS job
                        WHERE job.source_id = source.id
                          AND job.status IN ('pending', 'running')
                  )
                ON CONFLICT DO NOTHING
                RETURNING *
                """
            ).fetchall()

    def claim_next_job(self, worker_instance: str):
        with self.connection() as conn:
            return conn.execute(
                """
                WITH next_job AS (
                    SELECT id
                    FROM rag_metadata.google_drive_sync_jobs
                    WHERE status = 'pending'
                      AND attempt_count < %s
                    ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE rag_metadata.google_drive_sync_jobs AS job
                SET status = 'running',
                    worker_instance = %s,
                    started_at = COALESCE(started_at, NOW()),
                    finished_at = NULL,
                    heartbeat_at = NOW(),
                    attempt_count = attempt_count + 1,
                    error_message = '',
                    updated_at = NOW()
                FROM next_job
                WHERE job.id = next_job.id
                RETURNING job.*
                """,
                (MAX_JOB_ATTEMPTS, worker_instance),
            ).fetchone()

    def heartbeat_job(
        self,
        job_id: int,
        worker_instance: str,
        *,
        message: str | None = None,
    ):
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.google_drive_sync_jobs
                SET heartbeat_at = NOW(),
                    message = COALESCE(%s::TEXT, message),
                    updated_at = NOW()
                WHERE id = %s
                  AND status = 'running'
                  AND worker_instance = %s
                RETURNING *
                """,
                (message, job_id, worker_instance),
            ).fetchone()

    def finish_job(self, job_id: int, result: dict):
        final_status = result["status"]
        if final_status not in {"completed", "partial"}:
            raise ValueError("Trạng thái kết thúc job không hợp lệ")

        scan = result["scan"]
        version = result["version_check"]
        imported = result["import"]
        moved = result["move"]
        message = (
            f"scan={scan['discovered']} checked={version['checked']} "
            f"imported={imported['completed']} moved={moved['moved']}"
        )

        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.google_drive_sync_jobs
                SET status = %s,
                    discovered_files = %s,
                    processed_files = %s,
                    imported_files = %s,
                    skipped_files = %s,
                    failed_files = %s,
                    message = %s,
                    error_message = '',
                    finished_at = NOW(),
                    heartbeat_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                  AND status = 'running'
                RETURNING *
                """,
                (
                    final_status,
                    scan["discovered"],
                    version["checked"],
                    imported["completed"],
                    version["skipped"],
                    result["failed"],
                    message,
                    job_id,
                ),
            ).fetchone()

    def fail_job(self, job_id: int, error_message: str):
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.google_drive_sync_jobs
                SET status = 'failed',
                    error_message = %s,
                    finished_at = NOW(),
                    heartbeat_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                  AND status IN ('pending', 'running')
                RETURNING *
                """,
                (error_message[:4000], job_id),
            ).fetchone()

    def mark_source_scanned(
        self,
        source_id: int,
        *,
        successful: bool,
        error_message: str = "",
    ):
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.google_drive_sources
                SET last_scan_at = NOW(),
                    last_success_at = CASE
                        WHEN %s THEN NOW()
                        ELSE last_success_at
                    END,
                    last_error = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (successful, error_message[:4000], source_id),
            ).fetchone()

    def recover_interrupted_jobs(self):
        with self.connection() as conn:
            failed = conn.execute(
                """
                UPDATE rag_metadata.google_drive_sync_jobs
                SET status = 'failed',
                    error_message = 'Vượt quá số lần khôi phục worker.',
                    finished_at = NOW(),
                    updated_at = NOW()
                WHERE status = 'running'
                  AND attempt_count >= %s
                RETURNING id
                """,
                (MAX_JOB_ATTEMPTS,),
            ).fetchall()
            recovered = conn.execute(
                """
                UPDATE rag_metadata.google_drive_sync_jobs
                SET status = 'pending',
                    worker_instance = '',
                    started_at = NULL,
                    finished_at = NULL,
                    heartbeat_at = NULL,
                    message = CASE
                        WHEN message = '' THEN 'Khôi phục sau khi worker khởi động lại.'
                        ELSE message || E'\nKhôi phục sau khi worker khởi động lại.'
                    END,
                    updated_at = NOW()
                WHERE status = 'running'
                RETURNING id
                """
            ).fetchall()
        return {
            "recovered": [row["id"] for row in recovered],
            "failed": [row["id"] for row in failed],
        }

    def acquire_worker_lock(self):
        conn = psycopg.connect(
            self.dsn,
            connect_timeout=5,
            row_factory=dict_row,
            autocommit=True,
        )
        row = conn.execute(
            "SELECT pg_try_advisory_lock(%s, %s) AS acquired",
            (DRIVE_WORKER_LOCK_KEY_1, DRIVE_WORKER_LOCK_KEY_2),
        ).fetchone()
        if not row or not row["acquired"]:
            conn.close()
            return None
        return conn

    @staticmethod
    def release_worker_lock(conn):
        if conn is None:
            return
        try:
            conn.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                (DRIVE_WORKER_LOCK_KEY_1, DRIVE_WORKER_LOCK_KEY_2),
            )
        finally:
            conn.close()
