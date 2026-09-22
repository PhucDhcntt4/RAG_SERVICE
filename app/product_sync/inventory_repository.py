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
                result = dict(claimed)
                result["trigger_type"] = trigger
                return result

    def complete(self, stats: dict):
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
        return self._with_next_run(row)

    def fail(self, message: str, *, retry_minutes: int = 15):
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
        return self._with_next_run(row)
