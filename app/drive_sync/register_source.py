from __future__ import annotations

import argparse
import json

import psycopg

from app.config import Settings
from app.drive_sync.departments import DepartmentMappingError
from app.drive_sync.google_client import GoogleDriveClient, GoogleDriveError
from app.drive_sync.repository import GoogleDriveSyncRepository
from app.drive_sync.source_service import DriveSourceError, DriveSourceService
from app.taxonomy_repository import TaxonomyRepository


def json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Đăng ký folder phòng ban Google Drive "
            "và tạo nhóm Public/Private"
        )
    )
    parser.add_argument(
        "--folder-id",
        required=True,
        help="Google Drive folder ID của phòng ban",
    )
    args = parser.parse_args()

    try:
        settings = Settings.load()

        taxonomy = TaxonomyRepository(settings)
        taxonomy.ready()

        repository = GoogleDriveSyncRepository(settings)
        repository.ready()

        service = DriveSourceService(
            drive_client=GoogleDriveClient(settings),
            drive_repository=repository,
            taxonomy_repository=taxonomy,
        )
        result = service.register_department(args.folder_id)
    except (
        GoogleDriveError,
        DepartmentMappingError,
        DriveSourceError,
        psycopg.Error,
    ) as exc:
        raise SystemExit(f"Đăng ký Google Drive thất bại: {exc}") from None

    print("Đăng ký nguồn Google Drive thành công.")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=json_default))


if __name__ == "__main__":
    main()
