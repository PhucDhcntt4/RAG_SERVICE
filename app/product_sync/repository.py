from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb
from psycopg.rows import dict_row


WORKER_LOCK_KEY_1 = 734211
WORKER_LOCK_KEY_2 = 260921


class ProductSyncRepository:
    def __init__(self, settings):
        if settings.database_url is None:
            raise RuntimeError("Thiếu DATABASE_URL cho Product Sync")
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
                CREATE TABLE IF NOT EXISTS rag_metadata.product_sync_settings (
                    id SMALLINT PRIMARY KEY CHECK (id = 1),
                    enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    sync_mode VARCHAR(20) NOT NULL DEFAULT 'existing'
                        CHECK (sync_mode IN ('existing', 'all_active')),
                    sync_time TIME NOT NULL DEFAULT TIME '02:00',
                    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Ho_Chi_Minh',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            conn.execute(
                """
                INSERT INTO rag_metadata.product_sync_settings(id)
                VALUES (1)
                ON CONFLICT (id) DO NOTHING
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_metadata.product_sync_jobs (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    trigger_type VARCHAR(20) NOT NULL
                        CHECK (trigger_type IN ('manual', 'scheduled')),
                    mode VARCHAR(20) NOT NULL
                        CHECK (mode IN ('existing', 'all_active')),
                    status VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'completed', 'failed')),
                    dry_run BOOLEAN NOT NULL DEFAULT TRUE,

                    total_products INTEGER NOT NULL DEFAULT 0,
                    selected_products INTEGER NOT NULL DEFAULT 0,
                    processed_products INTEGER NOT NULL DEFAULT 0,
                    skipped_products INTEGER NOT NULL DEFAULT 0,
                    failed_products INTEGER NOT NULL DEFAULT 0,
                    created_products INTEGER NOT NULL DEFAULT 0,
                    updated_products INTEGER NOT NULL DEFAULT 0,
                    inactivated_products INTEGER NOT NULL DEFAULT 0,
                    new_embeddings INTEGER NOT NULL DEFAULT 0,
                    removed_image_points INTEGER NOT NULL DEFAULT 0,

                    message TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',

                    dedupe_key VARCHAR(160) UNIQUE,

                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # Safe additive migration from Phase 1/2A jobs table.
            for definition in (
                "processed_products INTEGER NOT NULL DEFAULT 0",
                "created_products INTEGER NOT NULL DEFAULT 0",
                "updated_products INTEGER NOT NULL DEFAULT 0",
                "inactivated_products INTEGER NOT NULL DEFAULT 0",
                "new_embeddings INTEGER NOT NULL DEFAULT 0",
                "removed_image_points INTEGER NOT NULL DEFAULT 0",
            ):
                name = definition.split()[0]
                conn.execute(
                    f"ALTER TABLE rag_metadata.product_sync_jobs "
                    f"ADD COLUMN IF NOT EXISTS {definition}"
                )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_product_sync_jobs_status
                ON rag_metadata.product_sync_jobs(status, created_at, id)
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_metadata.product_sync_worker_state (
                    id SMALLINT PRIMARY KEY CHECK (id = 1),
                    instance_id VARCHAR(64) NOT NULL DEFAULT '',
                    current_job_id BIGINT,
                    started_at TIMESTAMPTZ,
                    heartbeat_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            conn.execute(
                """
                INSERT INTO rag_metadata.product_sync_worker_state(id)
                VALUES (1)
                ON CONFLICT (id) DO NOTHING
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_metadata.product_sync_changes (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    job_id BIGINT NOT NULL REFERENCES rag_metadata.product_sync_jobs(id)
                        ON DELETE CASCADE,
                    product_code VARCHAR(100) NOT NULL,
                    product_title TEXT NOT NULL DEFAULT '',
                    change_type VARCHAR(30) NOT NULL
                        CHECK (change_type IN (
                            'CREATED', 'UPDATED', 'INACTIVATED', 'STATUS_CHANGED'
                        )),
                    changes JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (job_id, product_code)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_product_sync_changes_job
                ON rag_metadata.product_sync_changes(job_id, id DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_product_sync_changes_code
                ON rag_metadata.product_sync_changes(product_code)
                """
            )

        return self.get_settings()

    def get_settings(self):
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM rag_metadata.product_sync_settings
                WHERE id = 1
                """
            ).fetchone()

    def update_settings(
        self,
        *,
        enabled: bool,
        sync_mode: str,
        sync_time,
        timezone: str,
    ):
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.product_sync_settings
                SET enabled=%s,
                    sync_mode=%s,
                    sync_time=%s,
                    timezone=%s,
                    updated_at=NOW()
                WHERE id=1
                RETURNING *
                """,
                (enabled, sync_mode, sync_time, timezone),
            ).fetchone()

    def enqueue(
        self,
        *,
        mode: str,
        trigger_type: str = "manual",
        dry_run: bool = True,
        dedupe_key: str | None = None,
    ):
        with self.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO rag_metadata.product_sync_jobs
                    (trigger_type, mode, dry_run, dedupe_key)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (dedupe_key) DO NOTHING
                RETURNING *
                """,
                (trigger_type, mode, dry_run, dedupe_key),
            ).fetchone()

            if row is not None or dedupe_key is None:
                return row

            return conn.execute(
                """
                SELECT *
                FROM rag_metadata.product_sync_jobs
                WHERE dedupe_key=%s
                """,
                (dedupe_key,),
            ).fetchone()

    def active_job(self):
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM rag_metadata.product_sync_jobs
                WHERE status IN ('pending', 'running')
                ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, id
                LIMIT 1
                """
            ).fetchone()

    def claim_next_job(self):
        with self.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    SELECT *
                    FROM rag_metadata.product_sync_jobs
                    WHERE status='pending'
                    ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """
                ).fetchone()

                if row is None:
                    return None

                return conn.execute(
                    """
                    UPDATE rag_metadata.product_sync_jobs
                    SET status='running',
                        started_at=COALESCE(started_at, NOW()),
                        finished_at=NULL,
                        updated_at=NOW(),
                        error_message=''
                    WHERE id=%s
                    RETURNING *
                    """,
                    (row["id"],),
                ).fetchone()

    def update_progress(
        self,
        job_id: int,
        *,
        total_products: int,
        selected_products: int,
        processed_products: int,
        skipped_products: int,
        failed_products: int = 0,
        created_products: int = 0,
        updated_products: int = 0,
        inactivated_products: int = 0,
        new_embeddings: int = 0,
        removed_image_points: int = 0,
        message: str = "",
    ):
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.product_sync_jobs
                SET total_products=%s,
                    selected_products=%s,
                    processed_products=%s,
                    skipped_products=%s,
                    failed_products=%s,
                    created_products=%s,
                    updated_products=%s,
                    inactivated_products=%s,
                    new_embeddings=%s,
                    removed_image_points=%s,
                    message=%s,
                    updated_at=NOW()
                WHERE id=%s
                RETURNING *
                """,
                (
                    total_products,
                    selected_products,
                    processed_products,
                    skipped_products,
                    failed_products,
                    created_products,
                    updated_products,
                    inactivated_products,
                    new_embeddings,
                    removed_image_points,
                    message[:4000],
                    job_id,
                ),
            ).fetchone()

    def complete(self, job_id: int, **progress):
        values = {
            "total_products": 0,
            "selected_products": 0,
            "processed_products": 0,
            "skipped_products": 0,
            "failed_products": 0,
            "created_products": 0,
            "updated_products": 0,
            "inactivated_products": 0,
            "new_embeddings": 0,
            "removed_image_points": 0,
            "message": "",
            **progress,
        }

        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.product_sync_jobs
                SET status='completed',
                    total_products=%s,
                    selected_products=%s,
                    processed_products=%s,
                    skipped_products=%s,
                    failed_products=%s,
                    created_products=%s,
                    updated_products=%s,
                    inactivated_products=%s,
                    new_embeddings=%s,
                    removed_image_points=%s,
                    message=%s,
                    finished_at=NOW(),
                    updated_at=NOW()
                WHERE id=%s
                RETURNING *
                """,
                (
                    values["total_products"],
                    values["selected_products"],
                    values["processed_products"],
                    values["skipped_products"],
                    values["failed_products"],
                    values["created_products"],
                    values["updated_products"],
                    values["inactivated_products"],
                    values["new_embeddings"],
                    values["removed_image_points"],
                    str(values["message"])[:4000],
                    job_id,
                ),
            ).fetchone()

    def fail(self, job_id: int, error_message: str):
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.product_sync_jobs
                SET status='failed',
                    error_message=%s,
                    finished_at=NOW(),
                    updated_at=NOW()
                WHERE id=%s
                RETURNING *
                """,
                (error_message[:2000], job_id),
            ).fetchone()

    def latest_job(self):
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM rag_metadata.product_sync_jobs
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()

    def history(self, limit: int = 20):
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM rag_metadata.product_sync_jobs
                ORDER BY id DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()

    def history_page(self, page: int = 1, page_size: int = 5):
        page = max(int(page), 1)
        page_size = min(max(int(page_size), 1), 100)

        with self.connection() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) AS total FROM rag_metadata.product_sync_jobs"
            ).fetchone()
            total = int(total_row["total"] if total_row else 0)
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            rows = conn.execute(
                """
                SELECT j.*,
                       (
                           SELECT COUNT(*)
                           FROM rag_metadata.product_sync_changes c
                           WHERE c.job_id=j.id
                       ) AS change_count
                FROM rag_metadata.product_sync_jobs j
                ORDER BY j.id DESC
                LIMIT %s OFFSET %s
                """,
                (page_size, (page - 1) * page_size),
            ).fetchall()

        return {
            "jobs": rows,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }

    def save_product_change(self, job_id: int, change: dict):
        with self.connection() as conn:
            return conn.execute(
                """
                INSERT INTO rag_metadata.product_sync_changes (
                    job_id,
                    product_code,
                    product_title,
                    change_type,
                    changes
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (job_id, product_code)
                DO UPDATE SET
                    product_title=EXCLUDED.product_title,
                    change_type=EXCLUDED.change_type,
                    changes=EXCLUDED.changes
                RETURNING *
                """,
                (
                    int(job_id),
                    str(change.get("product_code") or "")[:100],
                    str(change.get("product_title") or ""),
                    str(change.get("change_type") or "UPDATED"),
                    Jsonb(change.get("changes") or {}),
                ),
            ).fetchone()

    def product_changes_page(
        self,
        job_id: int,
        *,
        page: int = 1,
        page_size: int = 10,
    ):
        page = max(int(page), 1)
        page_size = min(max(int(page_size), 1), 100)

        with self.connection() as conn:
            total_row = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM rag_metadata.product_sync_changes
                WHERE job_id=%s
                """,
                (int(job_id),),
            ).fetchone()
            total = int(total_row["total"] if total_row else 0)
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            rows = conn.execute(
                """
                SELECT *
                FROM rag_metadata.product_sync_changes
                WHERE job_id=%s
                ORDER BY id DESC
                LIMIT %s OFFSET %s
                """,
                (int(job_id), page_size, (page - 1) * page_size),
            ).fetchall()

        return {
            "job_id": int(job_id),
            "changes": rows,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }

    def maybe_enqueue_scheduled(
        self,
        *,
        dry_run: bool = False,
        now_utc: datetime | None = None,
    ):
        settings = self.get_settings()
        if not settings or not settings["enabled"]:
            return None

        now_utc = now_utc or datetime.now(UTC)
        zone = ZoneInfo(settings["timezone"])
        local_now = now_utc.astimezone(zone)

        target = settings["sync_time"]
        target_seconds = target.hour * 3600 + target.minute * 60 + target.second
        now_seconds = local_now.hour * 3600 + local_now.minute * 60 + local_now.second

        if now_seconds < target_seconds:
            return None

        dedupe_key = f"daily:{local_now.date().isoformat()}:{settings['sync_mode']}"

        return self.enqueue(
            mode=settings["sync_mode"],
            trigger_type="scheduled",
            dry_run=dry_run,
            dedupe_key=dedupe_key,
        )

    def acquire_worker_lock(self):
        conn = psycopg.connect(
            self.dsn,
            connect_timeout=5,
            row_factory=dict_row,
            autocommit=True,
        )
        row = conn.execute(
            "SELECT pg_try_advisory_lock(%s, %s) AS acquired",
            (WORKER_LOCK_KEY_1, WORKER_LOCK_KEY_2),
        ).fetchone()

        if not row or not row["acquired"]:
            conn.close()
            return None

        return conn

    def release_worker_lock(self, conn):
        if conn is None:
            return
        try:
            conn.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                (WORKER_LOCK_KEY_1, WORKER_LOCK_KEY_2),
            )
        finally:
            conn.close()

    def recover_interrupted_jobs(self):
        with self.connection() as conn:
            rows = conn.execute(
                """
                UPDATE rag_metadata.product_sync_jobs
                SET status='pending',
                    started_at=NULL,
                    finished_at=NULL,
                    message=CASE
                        WHEN message='' THEN 'Khôi phục sau khi worker khởi động lại.'
                        ELSE message || E'\nKhôi phục sau khi worker khởi động lại.'
                    END,
                    updated_at=NOW()
                WHERE status='running'
                RETURNING id
                """
            ).fetchall()
        return [row["id"] for row in rows]

    def heartbeat(self, instance_id: str, current_job_id: int | None = None):
        with self.connection() as conn:
            return conn.execute(
                """
                UPDATE rag_metadata.product_sync_worker_state
                SET instance_id=%s,
                    current_job_id=%s,
                    started_at=CASE
                        WHEN instance_id IS DISTINCT FROM %s OR started_at IS NULL
                            THEN NOW()
                        ELSE started_at
                    END,
                    heartbeat_at=NOW(),
                    updated_at=NOW()
                WHERE id=1
                RETURNING *
                """,
                (instance_id, current_job_id, instance_id),
            ).fetchone()

    def worker_status(self, online_within_seconds: int = 120):
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM rag_metadata.product_sync_worker_state
                WHERE id=1
                """
            ).fetchone()

        if row is None:
            return {"online": False}

        heartbeat = row.get("heartbeat_at")
        online = bool(
            heartbeat
            and datetime.now(UTC) - heartbeat <= timedelta(seconds=online_within_seconds)
        )

        return {**row, "online": online}
