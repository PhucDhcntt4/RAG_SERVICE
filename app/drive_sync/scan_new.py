from __future__ import annotations

import argparse
import json

import psycopg

from app.config import Settings
from app.drive_sync.departments import DepartmentMappingError
from app.drive_sync.discovery_service import DriveDiscoveryError, DriveDiscoveryService
from app.drive_sync.google_client import GoogleDriveClient, GoogleDriveError
from app.drive_sync.repository import GoogleDriveSyncRepository
from app.taxonomy_repository import TaxonomyRepository


def json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def main():
    parser = argparse.ArgumentParser(
        description="Quét metadata file trong PUBLIC/NEW và PRIVATE/NEW"
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
        taxonomy = TaxonomyRepository(settings)
        taxonomy.ready()

        service = DriveDiscoveryService(
            drive_client=GoogleDriveClient(settings),
            drive_repository=repository,
            taxonomy_repository=taxonomy,
        )
        result = service.scan_department(args.folder_id)
    except (
        GoogleDriveError,
        DepartmentMappingError,
        DriveDiscoveryError,
        psycopg.Error,
    ) as exc:
        raise SystemExit(f"Quét Google Drive thất bại: {exc}") from None

    print("Quét Google Drive thành công.")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=json_default))


if __name__ == "__main__":
    main()
