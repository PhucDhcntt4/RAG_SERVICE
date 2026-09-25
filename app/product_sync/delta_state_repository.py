from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.rows import dict_row


class ProductDeltaStateRepository:
    SYNC_NAME = "shopify_product_delta"
    OVERLAP_SECONDS = 120

    def __init__(self, settings):
        if settings.database_url is None:
            raise RuntimeError("Thiếu DATABASE_URL cho Product Delta Sync")

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
                CREATE TABLE IF NOT EXISTS rag_metadata.product_delta_state (
                    sync_name VARCHAR(100) PRIMARY KEY,
                    last_success_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_metadata.product_delta_product_map (
                    shopify_product_id TEXT PRIMARY KEY,
                    product_code TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_product_delta_map_code
                ON rag_metadata.product_delta_product_map(product_code)
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_metadata.product_delta_checkpoint_history (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    sync_name VARCHAR(100) NOT NULL,
                    previous_checkpoint TIMESTAMPTZ NOT NULL,
                    new_checkpoint TIMESTAMPTZ NOT NULL,
                    reason TEXT NOT NULL,
                    source VARCHAR(30) NOT NULL DEFAULT 'admin_ui',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_delta_checkpoint_history_created
                ON rag_metadata.product_delta_checkpoint_history(created_at DESC, id DESC)
                """
            )

    def checkpoint_status(self) -> dict | None:
        with self.connection() as conn:
            state = conn.execute(
                """
                SELECT sync_name, last_success_at, updated_at
                FROM rag_metadata.product_delta_state
                WHERE sync_name=%s
                """,
                (self.SYNC_NAME,),
            ).fetchone()
            if state is None:
                return None

            mapped = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM rag_metadata.product_delta_product_map
                """
            ).fetchone()
            latest = conn.execute(
                """
                SELECT previous_checkpoint, new_checkpoint, reason, source, created_at
                FROM rag_metadata.product_delta_checkpoint_history
                WHERE sync_name=%s
                ORDER BY id DESC
                LIMIT 1
                """,
                (self.SYNC_NAME,),
            ).fetchone()

        checkpoint = state["last_success_at"]
        return {
            **state,
            "query_start_at": checkpoint
            - timedelta(seconds=self.OVERLAP_SECONDS),
            "overlap_seconds": self.OVERLAP_SECONDS,
            "mapped_products": int(mapped["total"] if mapped else 0),
            "latest_adjustment": latest,
        }

    def set_checkpoint(
        self,
        *,
        last_success_at: datetime,
        reason: str,
        source: str = "admin_ui",
    ) -> dict:
        if last_success_at.tzinfo is None or last_success_at.utcoffset() is None:
            raise ValueError("Checkpoint phải có múi giờ")

        normalized = last_success_at.astimezone(UTC)
        if normalized > datetime.now(UTC):
            raise ValueError("Checkpoint không được nằm trong tương lai")

        reason = str(reason or "").strip()
        if len(reason) < 3:
            raise ValueError("Lý do điều chỉnh phải có ít nhất 3 ký tự")

        with self.connection() as conn:
            with conn.transaction():
                current = conn.execute(
                    """
                    SELECT last_success_at
                    FROM rag_metadata.product_delta_state
                    WHERE sync_name=%s
                    FOR UPDATE
                    """,
                    (self.SYNC_NAME,),
                ).fetchone()
                if current is None:
                    raise RuntimeError(
                        "Delta Sync chưa bootstrap; không thể điều chỉnh checkpoint."
                    )

                conn.execute(
                    """
                    INSERT INTO rag_metadata.product_delta_checkpoint_history (
                        sync_name,
                        previous_checkpoint,
                        new_checkpoint,
                        reason,
                        source
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        self.SYNC_NAME,
                        current["last_success_at"],
                        normalized,
                        reason[:1000],
                        str(source or "admin_ui")[:30],
                    ),
                )
                conn.execute(
                    """
                    UPDATE rag_metadata.product_delta_state
                    SET last_success_at=%s,
                        updated_at=NOW()
                    WHERE sync_name=%s
                    """,
                    (normalized, self.SYNC_NAME),
                )

        return self.checkpoint_status()

    def load(self) -> dict:
        """Load the Delta checkpoint and Shopify Product ID -> SKU map."""
        with self.connection() as conn:
            state = conn.execute(
                """
                SELECT last_success_at
                FROM rag_metadata.product_delta_state
                WHERE sync_name = %s
                """,
                (self.SYNC_NAME,),
            ).fetchone()

            if state is None:
                raise RuntimeError(
                    "Delta Sync chưa bootstrap trong PostgreSQL. Chạy: "
                    "python -m app.product_sync.delta_sync --bootstrap"
                )

            rows = conn.execute(
                """
                SELECT shopify_product_id, product_code
                FROM rag_metadata.product_delta_product_map
                """
            ).fetchall()

        return {
            "last_success_at": state["last_success_at"],
            "product_map": {
                row["shopify_product_id"]: row["product_code"]
                for row in rows
            },
        }

    def replace_state(
        self,
        *,
        last_success_at: datetime,
        product_map: dict[str, str],
    ):
        """Replace the complete baseline; used only by explicit bootstrap."""
        rows = [
            (str(shopify_id).strip(), str(product_code).strip())
            for shopify_id, product_code in product_map.items()
            if str(shopify_id).strip() and str(product_code).strip()
        ]

        with self.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO rag_metadata.product_delta_state (
                        sync_name,
                        last_success_at,
                        updated_at
                    )
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (sync_name)
                    DO UPDATE SET
                        last_success_at = EXCLUDED.last_success_at,
                        updated_at = NOW()
                    """,
                    (self.SYNC_NAME, last_success_at),
                )

                conn.execute(
                    "DELETE FROM rag_metadata.product_delta_product_map"
                )

                if rows:
                    with conn.cursor() as cursor:
                        cursor.executemany(
                            """
                            INSERT INTO rag_metadata.product_delta_product_map (
                                shopify_product_id,
                                product_code,
                                updated_at
                            )
                            VALUES (%s, %s, NOW())
                            """,
                            rows,
                        )

        return {
            "last_success_at": last_success_at,
            "mapped_products": len(rows),
        }

    def commit_success(
        self,
        *,
        cutoff: datetime,
        upserts: dict[str, str],
        removed_ids: set[str],
    ):
        """Atomically update changed mappings and advance a successful checkpoint."""
        rows = [
            (str(shopify_id).strip(), str(product_code).strip())
            for shopify_id, product_code in upserts.items()
            if str(shopify_id).strip() and str(product_code).strip()
        ]
        removed = sorted(
            str(shopify_id).strip()
            for shopify_id in removed_ids
            if str(shopify_id).strip()
        )

        with self.connection() as conn:
            with conn.transaction():
                if rows:
                    with conn.cursor() as cursor:
                        cursor.executemany(
                            """
                            INSERT INTO rag_metadata.product_delta_product_map (
                                shopify_product_id,
                                product_code,
                                updated_at
                            )
                            VALUES (%s, %s, NOW())
                            ON CONFLICT (shopify_product_id)
                            DO UPDATE SET
                                product_code = EXCLUDED.product_code,
                                updated_at = NOW()
                            """,
                            rows,
                        )

                if removed:
                    conn.execute(
                        """
                        DELETE FROM rag_metadata.product_delta_product_map
                        WHERE shopify_product_id = ANY(%s)
                        """,
                        (removed,),
                    )

                updated = conn.execute(
                    """
                    UPDATE rag_metadata.product_delta_state
                    SET last_success_at = %s,
                        updated_at = NOW()
                    WHERE sync_name = %s
                    """,
                    (cutoff, self.SYNC_NAME),
                )

                if updated.rowcount != 1:
                    raise RuntimeError(
                        "Không tìm thấy Delta checkpoint trong PostgreSQL; "
                        "hãy chạy --bootstrap trước."
                    )

        return {
            "last_success_at": cutoff,
            "upserted_mappings": len(rows),
            "removed_mappings": len(removed),
        }
