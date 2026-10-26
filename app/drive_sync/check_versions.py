from __future__ import annotations

import argparse
import json

import psycopg

from app.config import Settings
from app.drive_sync.google_client import GoogleDriveClient, GoogleDriveError
from app.drive_sync.repository import GoogleDriveSyncRepository
from app.drive_sync.version_service import DriveVersionService


def main():
    parser = argparse.ArgumentParser(
        description="Tải file, tính SHA-256 và kiểm tra version"
    )
    parser.add_argument(
        "--folder-id",
        required=True,
        help="Google Drive folder ID của phòng ban đã đăng ký",
    )
    args = parser.parse_args()

    try:
        settings = Settings.load()
        repository = GoogleDriveSyncRepository(settings)
        repository.ready()
        source = repository.get_source_by_folder_id(args.folder_id)
        if source is None:
            raise RuntimeError("Folder phòng ban chưa được đăng ký")

        service = DriveVersionService(
            drive_client=GoogleDriveClient(settings),
            drive_repository=repository,
            max_file_bytes=settings.rag_max_upload_bytes,
        )
        result = service.evaluate_source(source["id"])
    except (GoogleDriveError, psycopg.Error, RuntimeError) as exc:
        raise SystemExit(f"Kiểm tra version thất bại: {exc}") from None

    print("Kiểm tra version Google Drive thành công.")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
