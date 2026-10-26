from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

FILE_FIELDS = (
    "id,"
    "name,"
    "mimeType,"
    "driveId,"
    "parents,"
    "createdTime,"
    "modifiedTime,"
    "md5Checksum,"
    "size,"
    "trashed"
)


class GoogleDriveError(RuntimeError):
    pass


class GoogleDriveNotFound(GoogleDriveError):
    pass


class GoogleDriveStructureError(GoogleDriveError):
    pass


class GoogleDriveClient:
    def __init__(self, settings):
        credentials_path: Path = settings.google_drive_credentials_path

        if not credentials_path.is_file():
            raise GoogleDriveError(
                f"Không tìm thấy Google Drive credential: {credentials_path}"
            )

        try:
            credentials = (
                service_account.Credentials.from_service_account_file(
                    str(credentials_path),
                    scopes=[DRIVE_SCOPE],
                )
            )

            self.service = build(
                "drive",
                "v3",
                credentials=credentials,
                cache_discovery=False,
            )
        except (OSError, ValueError, KeyError) as exc:
            raise GoogleDriveError(
                f"Không đọc được service account: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _http_error_message(exc: HttpError) -> str:
        status = getattr(exc.resp, "status", None)

        if status == 403:
            return (
                "Google Drive từ chối truy cập. "
                "Kiểm tra quyền của service account."
            )

        if status == 404:
            return (
                "Không tìm thấy folder hoặc service account "
                "chưa được cấp quyền."
            )

        return f"Google Drive API lỗi HTTP {status or 'không xác định'}"

    def get_item(self, file_id: str) -> dict[str, Any]:
        value = (file_id or "").strip()

        if not value:
            raise GoogleDriveError("Google Drive file ID không được để trống")

        try:
            return (
                self.service.files()
                .get(
                    fileId=value,
                    fields=FILE_FIELDS,
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            raise GoogleDriveError(
                self._http_error_message(exc)
            ) from exc

    def get_folder(self, folder_id: str) -> dict[str, Any]:
        item = self.get_item(folder_id)

        if item.get("trashed"):
            raise GoogleDriveNotFound(
                f"Folder đang nằm trong thùng rác: {folder_id}"
            )

        if item.get("mimeType") != FOLDER_MIME_TYPE:
            raise GoogleDriveStructureError(
                f"ID {folder_id} không phải là folder Google Drive"
            )

        return item

    def list_children(self, parent_id: str) -> list[dict[str, Any]]:
        parent = self.get_folder(parent_id)
        drive_id = parent.get("driveId")

        params: dict[str, Any] = {
            "q": f"'{parent_id}' in parents and trashed = false",
            "spaces": "drive",
            "fields": f"nextPageToken,files({FILE_FIELDS})",
            "pageSize": 1000,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
            "orderBy": "folder,name",
        }

        if drive_id:
            params["corpora"] = "drive"
            params["driveId"] = drive_id

        rows: list[dict[str, Any]] = []
        page_token: str | None = None

        try:
            while True:
                if page_token:
                    params["pageToken"] = page_token
                else:
                    params.pop("pageToken", None)

                response = (
                    self.service.files()
                    .list(**params)
                    .execute()
                )

                rows.extend(response.get("files", []))
                page_token = response.get("nextPageToken")

                if not page_token:
                    break
        except HttpError as exc:
            raise GoogleDriveError(
                self._http_error_message(exc)
            ) from exc

        return rows

    def download_file(self, file_id: str, *, max_bytes: int) -> bytes:
        if max_bytes < 1:
            raise GoogleDriveError("Giới hạn dung lượng file không hợp lệ")

        item = self.get_item(file_id)
        if item.get("mimeType") == FOLDER_MIME_TYPE:
            raise GoogleDriveError("Không thể tải folder như một tài liệu")

        declared_size = item.get("size")
        if declared_size is not None and int(declared_size) > max_bytes:
            raise GoogleDriveError(
                f"File vượt quá giới hạn {max_bytes} bytes"
            )

        output = BytesIO()
        try:
            request = self.service.files().get_media(
                fileId=file_id,
                supportsAllDrives=True,
            )
            downloader = MediaIoBaseDownload(
                output,
                request,
                chunksize=1024 * 1024,
            )
            done = False
            while not done:
                _, done = downloader.next_chunk()
                if output.tell() > max_bytes:
                    raise GoogleDriveError(
                        f"File vượt quá giới hạn {max_bytes} bytes"
                    )
        except HttpError as exc:
            raise GoogleDriveError(self._http_error_message(exc)) from exc

        data = output.getvalue()
        if not data:
            raise GoogleDriveError("File Google Drive không có nội dung")
        return data

    def find_child_folder(
        self,
        parent_id: str,
        expected_name: str,
    ) -> dict[str, Any]:
        matches = [
            item
            for item in self.list_children(parent_id)
            if item.get("mimeType") == FOLDER_MIME_TYPE
            and str(item.get("name", "")).strip().casefold()
            == expected_name.strip().casefold()
        ]

        if not matches:
            raise GoogleDriveStructureError(
                f"Không tìm thấy folder '{expected_name}' "
                f"bên trong {parent_id}"
            )

        if len(matches) > 1:
            raise GoogleDriveStructureError(
                f"Có nhiều folder cùng tên '{expected_name}' "
                f"bên trong {parent_id}"
            )

        return matches[0]

    def inspect_department_folder(
        self,
        department_folder_id: str,
    ) -> dict[str, Any]:
        department = self.get_folder(department_folder_id)

        result: dict[str, Any] = {
            "department": {
                "id": department["id"],
                "name": department["name"],
                "drive_id": department.get("driveId"),
            },
            "scopes": {},
        }

        for scope_name in ("Public", "Private"):
            scope = self.find_child_folder(
                department_folder_id,
                scope_name,
            )

            stages: dict[str, Any] = {}

            for stage_name in ("NEW", "DONE", "ERROR"):
                stage = self.find_child_folder(
                    scope["id"],
                    stage_name,
                )

                stages[stage_name] = {
                    "id": stage["id"],
                    "name": stage["name"],
                }

            result["scopes"][scope_name.lower()] = {
                "id": scope["id"],
                "name": scope["name"],
                "stages": stages,
            }

        return result

    def move_item(
        self,
        file_id: str,
        *,
        source_folder_id: str,
        target_folder_id: str,
    ) -> dict[str, Any]:
        if not (source_folder_id or "").strip():
            raise GoogleDriveError("Source folder ID không được để trống")
        if not (target_folder_id or "").strip():
            raise GoogleDriveError("Target folder ID không được để trống")
        if source_folder_id == target_folder_id:
            raise GoogleDriveStructureError("Folder nguồn và folder đích bị trùng nhau")

        item = self.get_item(file_id)
        parents = set(item.get("parents") or [])

        # Lần chạy trước đã move thành công nhưng PostgreSQL
        # chưa kịp cập nhật. Nếu file vẫn còn cả hai parent thì xóa
        # NEW ở lần chạy này thay vì coi là đã hoàn tất.
        if target_folder_id in parents and source_folder_id not in parents:
            return item

        if source_folder_id not in parents and target_folder_id not in parents:
            raise GoogleDriveStructureError(
                "File không còn nằm trong folder NEW "
                "và cũng chưa nằm trong folder đích"
            )

        try:
            update_params: dict[str, Any] = {
                "fileId": file_id,
                "removeParents": source_folder_id,
                "fields": FILE_FIELDS,
                "supportsAllDrives": True,
            }
            if target_folder_id not in parents:
                update_params["addParents"] = target_folder_id

            return (
                self.service.files()
                .update(**update_params)
                .execute()
            )
        except HttpError as exc:
            raise GoogleDriveError(
                self._http_error_message(exc)
            ) from exc
