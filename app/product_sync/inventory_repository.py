from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import os

import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row

from app.config import ROOT


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_inventory_defaults() -> tuple[bool, int]:
    values = {**dotenv_values(ROOT / ".env"), **os.environ}
    enabled = _truthy(values.get("INVENTORY_SYNC_ENABLED"))
    raw = str(values.get("INVENTORY_SYNC_INTERVAL_HOURS") or "6").strip()
    try:
        interval_hours = int(raw)
    except ValueError:
        interval_hours = 6
    interval_hours = min(max(interval_hours, 1), 168)
    return enabled, interval_hours


class InventorySyncRepository:
    def __init__(self, settings):
        if settings.database_url is None:
            raise RuntimeError("Thiếu DATABASE_URL cho Inventory Sync")
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
        default_enabled, default_interval = load_inventory_defaults()
        with self.connection() as conn:
            conn.execute("CREATE SCHEMA IF NOT EXISTS rag_metadata")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_metadata.product_inventory_sync_state (
                    id SMALLINT PRIMARY KEY CHECK (id = 1),
                    enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    interval_hours SMALLINT NOT NULL DEFAULT 6
                        CHECK (interval_hours BETWEEN 1 AND 168),
                    run_requested BOOLEAN NOT NULL DEFAULT FALSE,
                    running BOOLEAN NOT NULL DEFAULT FALSE,
                    last_trigger VARCHAR(20) NOT NULL DEFAULT '',
                    last_started_at TIMESTAMPTZ,
                    last_success_at TIMESTAMPTZ,
                    last_finished_at TIMESTAMPTZ,
                    retry_after_at TIMESTAMPTZ,
                    last_checked_products INTEGER NOT NULL DEFAULT 0,
                    last_updated_products INTEGER NOT NULL DEFAULT 0,
                    last_checked_variants INTEGER NOT NULL DEFAULT 0,
                    last_changed_variants INTEGER NOT NULL DEFAULT 0,
                    last_missing_products INTEGER NOT NULL DEFAULT 0,
                    last_missing_variants INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                """
                INSERT INTO rag_metadata.product_inventory_sync_state(
                    id, enabled, interval_hours
                )
                VALUES (1, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (default_enabled, default_interval),
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_metadata.product_inventory_sync_runs (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    trigger_type VARCHAR(20) NOT NULL
                        CHECK (trigger_type IN ('manual', 'scheduled')),
                    status VARCHAR(20) NOT NULL
                        CHECK (status IN ('running', 'completed', 'failed')),

                    shopify_products INTEGER NOT NULL DEFAULT 0,
                    shopify_variants INTEGER NOT NULL DEFAULT 0,
                    skipped_without_code INTEGER NOT NULL DEFAULT 0,
                    checked_products INTEGER NOT NULL DEFAULT 0,
                    updated_products INTEGER NOT NULL DEFAULT 0,
                    checked_variants INTEGER NOT NULL DEFAULT 0,
                    changed_variants INTEGER NOT NULL DEFAULT 0,
                    missing_products INTEGER NOT NULL DEFAULT 0,
                    missing_variants INTEGER NOT NULL DEFAULT 0,

                    error_message TEXT NOT NULL DEFAULT '',
                    report_path TEXT NOT NULL DEFAULT '',
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_product_inventory_runs_started
                ON rag_metadata.product_inventory_sync_runs(started_at DESC, id DESC)
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_metadata.product_inventory_sync_changes (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    run_id BIGINT NOT NULL
                        REFERENCES rag_metadata.product_inventory_sync_runs(id)
                        ON DELETE CASCADE,
                    product_code VARCHAR(100) NOT NULL,
                    product_title TEXT NOT NULL DEFAULT '',
                    variant_key VARCHAR(200) NOT NULL,
                    variant_title TEXT NOT NULL DEFAULT '',
                    sku VARCHAR(150) NOT NULL DEFAULT '',
                    before_quantity INTEGER,
                    after_quantity INTEGER,
                    before_available BOOLEAN,
                    after_available BOOLEAN,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (run_id, product_code, variant_key)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_inventory_sync_changes_run
                ON rag_metadata.product_inventory_sync_changes(run_id, id DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_inventory_sync_changes_product
                ON rag_metadata.product_inventory_sync_changes(product_code)
                """
            )
        return self.get_status()

    @staticmethod
    def _with_next_run(row: dict | None):
        if row is None:
            return None
        result = dict(row)
        now = datetime.now(UTC)
        if result.get("run_requested"):
            next_run = now
        elif result.get("running"):
            next_run = None
        elif result.get("retry_after_at") and result["retry_after_at"] > now:
            next_run = result["retry_after_at"]
        elif result.get("enabled"):
            last_success = result.get("last_success_at")
            next_run = (
                last_success + timedelta(hours=int(result["interval_hours"]))
                if last_success
                else now
            )
        else:
            next_run = None
        result["next_run_at"] = next_run
        return result

    def get_status(self):
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM rag_metadata.product_inventory_sync_state
                WHERE id=1
                """
            ).fetchone()
        return self._with_next_run(row)

    def update_settings(self, *, enabled: bool, interval_hours: int):
        interval_hours = min(max(int(interval_hours), 1), 168)
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE rag_metadata.product_inventory_sync_state
                SET enabled=%s,
                    interval_hours=%s,
                    updated_at=NOW()
                WHERE id=1
                RETURNING *
                """,
                (enabled, interval_hours),
            ).fetchone()
        return self._with_next_run(row)

    def request_run(self):
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE rag_metadata.product_inventory_sync_state
                SET run_requested=TRUE,
                    retry_after_at=NULL,
                    updated_at=NOW()
                WHERE id=1
                RETURNING *
                """
            ).fetchone()
        return self._with_next_run(row)

    def recover_interrupted(self):
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE rag_metadata.product_inventory_sync_runs
                SET status='failed',
                    error_message=CASE
                        WHEN error_message='' THEN 'Worker khởi động lại khi job đang chạy.'
                        ELSE error_message
                    END,
                    finished_at=NOW(),
                    updated_at=NOW()
                WHERE status='running'
                """
            )
            row = conn.execute(
                """
                UPDATE rag_metadata.product_inventory_sync_state
                SET running=FALSE,
                    run_requested=CASE WHEN running THEN TRUE ELSE run_requested END,
                    last_error=CASE
                        WHEN running THEN 'Khôi phục Inventory Sync sau khi worker khởi động lại.'
                        ELSE last_error
                    END,
                    updated_at=NOW()
                WHERE id=1
                RETURNING *
                """
            ).fetchone()
        return self._with_next_run(row)

    def claim_due(self, now_utc: datetime | None = None):
        now_utc = now_utc or datetime.now(UTC)
        with self.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    SELECT *
                    FROM rag_metadata.product_inventory_sync_state
                    WHERE id=1
                    FOR UPDATE
                    """
                ).fetchone()
                if row is None or row["running"]:
                    return None

                manual = bool(row["run_requested"])
                retry_after = row.get("retry_after_at")
                retry_blocked = bool(retry_after and retry_after > now_utc)
                last_success = row.get("last_success_at")
                due_at = (
                    last_success + timedelta(hours=int(row["interval_hours"]))
                    if last_success
                    else now_utc
                )
                scheduled_due = bool(row["enabled"] and not retry_blocked and now_utc >= due_at)

                if not manual and not scheduled_due:
                    return None

                trigger = "manual" if manual else "scheduled"
                claimed = conn.execute(
                    """
                    UPDATE rag_metadata.product_inventory_sync_state
                    SET running=TRUE,
                        run_requested=FALSE,
                        last_trigger=%s,
                        last_started_at=NOW(),
                        last_finished_at=NULL,
                        last_error='',
                        updated_at=NOW()
                    WHERE id=1
                    RETURNING *
                    """,
                    (trigger,),
                ).fetchone()

                run = conn.execute(
                    """
                    INSERT INTO rag_metadata.product_inventory_sync_runs (
                        trigger_type,
                        status,
                        started_at
                    )
                    VALUES (%s, 'running', NOW())
                    RETURNING id
                    """,
                    (trigger,),
                ).fetchone()
                result = dict(claimed)
                result["trigger_type"] = trigger
                result["run_id"] = run["id"]
                return result

    def complete(
        self,
        stats: dict,
        *,
        run_id: int | None = None,
        report_path: str = "",
    ):
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE rag_metadata.product_inventory_sync_state
                SET running=FALSE,
                    last_success_at=NOW(),
                    last_finished_at=NOW(),
                    retry_after_at=NULL,
                    last_checked_products=%s,
                    last_updated_products=%s,
                    last_checked_variants=%s,
                    last_changed_variants=%s,
                    last_missing_products=%s,
                    last_missing_variants=%s,
                    last_error='',
                    updated_at=NOW()
                WHERE id=1
                RETURNING *
                """,
                (
                    int(stats.get("checked_products") or 0),
                    int(stats.get("updated_products") or 0),
                    int(stats.get("checked_variants") or 0),
                    int(stats.get("changed_variants") or 0),
                    int(stats.get("missing_products") or 0),
                    int(stats.get("missing_variants") or 0),
                ),
            ).fetchone()

            if run_id is not None:
                updated_run = conn.execute(
                    """
                    UPDATE rag_metadata.product_inventory_sync_runs
                    SET status='completed',
                        shopify_products=%s,
                        shopify_variants=%s,
                        skipped_without_code=%s,
                        checked_products=%s,
                        updated_products=%s,
                        checked_variants=%s,
                        changed_variants=%s,
                        missing_products=%s,
                        missing_variants=%s,
                        error_message='',
                        report_path=%s,
                        finished_at=NOW(),
                        updated_at=NOW()
                    WHERE id=%s
                    """,
                    (
                        int(stats.get("shopify_products") or 0),
                        int(stats.get("shopify_variants") or 0),
                        int(stats.get("skipped_without_code") or 0),
                        int(stats.get("checked_products") or 0),
                        int(stats.get("updated_products") or 0),
                        int(stats.get("checked_variants") or 0),
                        int(stats.get("changed_variants") or 0),
                        int(stats.get("missing_products") or 0),
                        int(stats.get("missing_variants") or 0),
                        str(report_path or "")[:2000],
                        run_id,
                    ),
                )
                if updated_run.rowcount != 1:
                    raise RuntimeError(
                        f"Không tìm thấy Inventory Sync run #{run_id}"
                    )
        return self._with_next_run(row)

    def fail(
        self,
        message: str,
        *,
        run_id: int | None = None,
        retry_minutes: int = 15,
    ):
        retry_minutes = min(max(int(retry_minutes), 1), 1440)
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE rag_metadata.product_inventory_sync_state
                SET running=FALSE,
                    last_finished_at=NOW(),
                    retry_after_at=NOW() + (%s * INTERVAL '1 minute'),
                    last_error=%s,
                    updated_at=NOW()
                WHERE id=1
                RETURNING *
                """,
                (retry_minutes, message[:2000]),
            ).fetchone()

            if run_id is not None:
                updated_run = conn.execute(
                    """
                    UPDATE rag_metadata.product_inventory_sync_runs
                    SET status='failed',
                        error_message=%s,
                        finished_at=NOW(),
                        updated_at=NOW()
                    WHERE id=%s
                    """,
                    (message[:2000], run_id),
                )
                if updated_run.rowcount != 1:
                    raise RuntimeError(
                        f"Không tìm thấy Inventory Sync run #{run_id}"
                    )
        return self._with_next_run(row)

    def history(self, limit: int = 20):
        limit = min(max(int(limit), 1), 100)
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT *
                FROM rag_metadata.product_inventory_sync_runs
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
                """
                SELECT COUNT(*) AS total
                FROM rag_metadata.product_inventory_sync_runs
                """
            ).fetchone()
            total = int(total_row["total"] if total_row else 0)
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            rows = conn.execute(
                """
                SELECT r.*,
                       (
                           SELECT COUNT(*)
                           FROM rag_metadata.product_inventory_sync_changes c
                           WHERE c.run_id=r.id
                       ) AS change_count
                FROM rag_metadata.product_inventory_sync_runs r
                ORDER BY r.id DESC
                LIMIT %s OFFSET %s
                """,
                (page_size, (page - 1) * page_size),
            ).fetchall()

        return {
            "runs": rows,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }

    def save_changes(self, run_id: int, changes: list[dict]):
        if not changes:
            return 0

        values = [
            (
                int(run_id),
                str(row.get("product_code") or "")[:100],
                str(row.get("product_title") or ""),
                str(row.get("variant_key") or "")[:200],
                str(row.get("variant_title") or ""),
                str(row.get("sku") or "")[:150],
                row.get("before_quantity"),
                row.get("after_quantity"),
                row.get("before_available"),
                row.get("after_available"),
            )
            for row in changes
        ]

        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO rag_metadata.product_inventory_sync_changes (
                        run_id,
                        product_code,
                        product_title,
                        variant_key,
                        variant_title,
                        sku,
                        before_quantity,
                        after_quantity,
                        before_available,
                        after_available
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id, product_code, variant_key)
                    DO UPDATE SET
                        product_title=EXCLUDED.product_title,
                        variant_title=EXCLUDED.variant_title,
                        sku=EXCLUDED.sku,
                        before_quantity=EXCLUDED.before_quantity,
                        after_quantity=EXCLUDED.after_quantity,
                        before_available=EXCLUDED.before_available,
                        after_available=EXCLUDED.after_available
                    """,
                    values,
                )
        return len(values)

    def changes_page(
        self,
        run_id: int,
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
                FROM rag_metadata.product_inventory_sync_changes
                WHERE run_id=%s
                """,
                (int(run_id),),
            ).fetchone()
            total = int(total_row["total"] if total_row else 0)
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            rows = conn.execute(
                """
                SELECT *
                FROM rag_metadata.product_inventory_sync_changes
                WHERE run_id=%s
                ORDER BY id DESC
                LIMIT %s OFFSET %s
                """,
                (int(run_id), page_size, (page - 1) * page_size),
            ).fetchall()

        return {
            "run_id": int(run_id),
            "changes": rows,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }
