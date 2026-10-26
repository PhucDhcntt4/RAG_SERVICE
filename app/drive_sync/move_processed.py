from __future__ import annotations

import argparse
import json

import psycopg

from app.config import Settings
from app.drive_sync.google_client import GoogleDriveClient, GoogleDriveError
from app.drive_sync.move_service import DriveMoveError, DriveMoveService
from app.drive_sync.repository import GoogleDriveSyncRepository


def main():
    parser = argparse.ArgumentParser(
        description="Di chuyển file Google Drive đã xử lý khỏi NEW"
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
            raise DriveMoveError("Folder phòng ban chưa được đăng ký")

        service = DriveMoveService(
            drive_client=GoogleDriveClient(settings),
            drive_repository=repository,
        )
        result = service.move_source(source["id"])
    except (DriveMoveError, GoogleDriveError, psycopg.Error) as exc:
        raise SystemExit(f"Di chuyển file Google Drive thất bại: {exc}") from None

    print("Di chuyển file Google Drive hoàn tất.")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
