from __future__ import annotations

import argparse
import json

from app.config import Settings
from app.drive_sync.google_client import (
    GoogleDriveClient,
    GoogleDriveError,
)


def main():
    parser = argparse.ArgumentParser(
        description="Kiểm tra cấu trúc folder phòng ban trên Google Drive"
    )

    parser.add_argument(
        "--folder-id",
        required=True,
        help="Google Drive folder ID của phòng ban, ví dụ HR–Nhân sự",
    )

    args = parser.parse_args()

    try:
        settings = Settings.load()
        client = GoogleDriveClient(settings)

        result = client.inspect_department_folder(
            args.folder_id
        )
    except GoogleDriveError as exc:
        raise SystemExit(f"Google Drive kiểm tra thất bại: {exc}") from None

    print("Google Drive kết nối thành công.")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()