from __future__ import annotations

from app.drive_sync.google_client import FOLDER_MIME_TYPE


class DriveMoveError(RuntimeError):
    pass


class DriveMoveService:
    """Move terminal Google Drive files out of NEW and persist the result."""

    def __init__(self, *, drive_client, drive_repository):
        self.drive = drive_client
        self.repository = drive_repository

    @staticmethod
    def _target_stage(status: str) -> str:
        if status in {"completed", "skipped"}:
            return "DONE"
        if status == "error":
            return "ERROR"
        raise DriveMoveError(f"Trạng thái '{status}' chưa kết thúc")

    def move_document(self, document: dict, structure: dict):
        if document["mime_type"] == FOLDER_MIME_TYPE:
            raise DriveMoveError(
                "Không tự động di chuyển folder con; hãy xử lý folder này thủ công"
            )

        scope = document["drive_scope"]
        if scope not in {"public", "private"}:
            raise DriveMoveError(f"Phạm vi Google Drive không hợp lệ: {scope}")

        target_stage = self._target_stage(document["status"])
        stages = structure["scopes"][scope]["stages"]
        self.drive.move_item(
            document["drive_file_id"],
            source_folder_id=stages["NEW"]["id"],
            target_folder_id=stages[target_stage]["id"],
        )
        moved = self.repository.mark_document_moved(
            document["id"], drive_stage=target_stage
        )
        return {
            "document_id": moved["id"],
            "drive_file_id": moved["drive_file_id"],
            "file_name": moved["original_file_name"],
            "status": moved["status"],
            "drive_stage": moved["drive_stage"],
        }

    def move_source(self, source_id: int):
        source = self.repository.get_source(source_id)
        if source is None:
            raise DriveMoveError("Không tìm thấy nguồn Google Drive")

        structure = self.drive.inspect_department_folder(source["folder_id"])
        documents = self.repository.list_documents_pending_move(source_id)
        result = {
            "source_id": source_id,
            "total": len(documents),
            "moved": 0,
            "failed": 0,
            "items": [],
        }

        for document in documents:
            try:
                item = self.move_document(document, structure)
                result["moved"] += 1
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                self.repository.record_move_error(document["id"], message)
                item = {
                    "document_id": document["id"],
                    "drive_file_id": document["drive_file_id"],
                    "file_name": document["original_file_name"],
                    "status": document["status"],
                    "drive_stage": "NEW",
                    "error_type": type(exc).__name__,
                    "error_message": message,
                }
                result["failed"] += 1
            result["items"].append(item)

        return result
