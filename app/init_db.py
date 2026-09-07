"""Explicit schema initialization; never runs automatically at API startup."""
import argparse

import psycopg

from app.config import ROOT, Settings


def main():
    parser = argparse.ArgumentParser(description="Tạo schema rag_service trong DATABASE_URL đã cấu hình")
    parser.add_argument("--confirm", action="store_true", help="Xác nhận đúng database đích")
    args = parser.parse_args()
    if not args.confirm:
        parser.error("Kiểm tra DATABASE_URL rồi thêm --confirm để tạo schema")
    settings = Settings.load()
    try:
        with psycopg.connect(settings.database_url.get_secret_value(), connect_timeout=5) as conn:
            for name in ("001_init.sql", "002_local_files.sql", "003_named_files.sql"):
                conn.execute((ROOT / "database" / name).read_text(encoding="utf-8"))
    except psycopg.Error as exc:
        raise SystemExit(f"Không tạo được schema ({type(exc).__name__}). Kiểm tra DB, pgvector và quyền truy cập.") from None
    print("Đã tạo/kiểm tra schema rag_service. Không thay đổi các bảng knowledge của bot.")


if __name__ == "__main__":
    main()
