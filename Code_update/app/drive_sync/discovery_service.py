from __future__ import annotations

from app.drive_sync.departments import build_group_code, resolve_department
from app.drive_sync.filename_parser import DriveFilenameError, parse_drive_filename
from app.drive_sync.google_client import FOLDER_MIME_TYPE


class DriveDiscoveryError(RuntimeError):
    pass


class DriveDiscoveryService:
    def __init__(
        self,
        *,
        drive_client,
        drive_repository,
        taxonomy_repository,
    ):
        self.drive = drive_client
        self.repository = drive_repository
        self.taxonomy = taxonomy_repository

    def _active_group(self, group_code: str):
        group = self.taxonomy.get_group_by_code(group_code)
        if group is None:
            raise DriveDiscoveryError(
                f"Chưa có nhóm '{group_code}'; hãy đăng ký nguồn trước"
            )
        if not group["is_active"]:
            raise DriveDiscoveryError(f"Nhóm '{group_code}' đang bị tạm dừng")
        return group

    def scan_department(self, folder_id: str, *, job_id: int | None = None):
        source = self.repository.get_source_by_folder_id(folder_id)
        if source is None:
            raise DriveDiscoveryError(
                "Folder phòng ban chưa được đăng ký trong PostgreSQL"
            )
        if not source["enabled"]:
            raise DriveDiscoveryError("Nguồn Google Drive đang bị tạm dừng")

        structure = self.drive.inspect_department_folder(folder_id)
        department = resolve_department(structure["department"]["name"])
        if department.code != source["department_code"]:
            raise DriveDiscoveryError(
                "Mã phòng ban trên Drive không trùng với PostgreSQL"
            )

        summary = {
            "source_id": source["id"],
            "department_code": department.code,
            "discovered": 0,
            "valid": 0,
            "invalid": 0,
            "items": [],
        }

        for scope in ("public", "private"):
            group_code = build_group_code(department, scope)
            group = self._active_group(group_code)
            new_folder_id = structure["scopes"][scope]["stages"]["NEW"]["id"]

            for item in self.drive.list_children(new_folder_id):
                summary["discovered"] += 1
                parsed = None
                error_code = ""
                error_message = ""

                if item.get("mimeType") == FOLDER_MIME_TYPE:
                    error_code = "FOLDER_NOT_ALLOWED"
                    error_message = "Không được đặt folder con trong thư mục NEW"
                else:
                    try:
                        parsed = parse_drive_filename(
                            str(item.get("name") or ""),
                            expected_department_code=department.code,
                        )
                    except DriveFilenameError as exc:
                        error_code = exc.code
                        error_message = str(exc)

                row = self.repository.record_scanned_document(
                    source_id=source["id"],
                    job_id=job_id,
                    group_id=group["id"],
                    drive_scope=scope,
                    drive_item=item,
                    parsed=parsed,
                    error_code=error_code,
                    error_message=error_message,
                )

                if error_code:
                    summary["invalid"] += 1
                else:
                    summary["valid"] += 1

                summary["items"].append(
                    {
                        "drive_file_id": item.get("id"),
                        "file_name": item.get("name"),
                        "scope": scope,
                        "status": row["status"],
                        "error_code": row["error_code"],
                    }
                )

        return summary
