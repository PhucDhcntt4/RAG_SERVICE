from __future__ import annotations

import argparse
import json

from app.drive_sync.filename_parser import (
    DriveFilenameError,
    parse_drive_filename,
)


def main():
    parser = argparse.ArgumentParser(
        description="Kiểm tra tên file dùng cho Google Drive Sync"
    )
    parser.add_argument(
        "--department",
        required=True,
        help="Mã folder phòng ban, ví dụ HR",
    )
    parser.add_argument(
        "--filename",
        required=True,
        help="Tên file cần kiểm tra",
    )
    args = parser.parse_args()

    try:
        result = parse_drive_filename(
            args.filename,
            expected_department_code=args.department,
        )
    except DriveFilenameError as exc:
        raise SystemExit(f"Tên file không hợp lệ [{exc.code}]: {exc}") from None

    print("Tên file hợp lệ.")
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
