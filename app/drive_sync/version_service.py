from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.drive_sync.google_client import GoogleDriveError


@dataclass(frozen=True)
class VersionDecision:
    action: str
    status: str = "discovered"
    error_code: str = ""
    error_message: str = ""


class DriveVersionService:
    def __init__(self, *, drive_client, drive_repository, max_file_bytes: int):
        self.drive = drive_client
        self.repository = drive_repository
        self.max_file_bytes = max_file_bytes

    @staticmethod
    def decide(document, completed_versions, content_sha256: str):
        version = int(document["document_version"])

        if not completed_versions:
            if version != 1:
                return VersionDecision(
                    action="",
                    status="error",
                    error_code="INITIAL_VERSION_MUST_BE_ONE",
                    error_message="Tài liệu mới phải bắt đầu từ V1",
                )
            return VersionDecision(action="CREATE")

        latest = completed_versions[0]
        same_version = next(
            (
                item
                for item in completed_versions
                if int(item["document_version"]) == version
            ),
            None,
        )

        if same_version is not None:
            if same_version["content_sha256"] == content_sha256:
                return VersionDecision(action="DUPLICATE", status="skipped")
            return VersionDecision(
                action="",
                status="error",
                error_code="VERSION_CONTENT_CONFLICT",
                error_message=(
                    f"{document['document_code']} V{version} đã tồn tại "
                    "nhưng có nội dung khác"
                ),
            )

        latest_version = int(latest["document_version"])
        if version < latest_version:
            return VersionDecision(
                action="",
                status="error",
                error_code="OLDER_VERSION",
                error_message=(
                    f"Version V{version} thấp hơn version hiện tại V{latest_version}"
                ),
            )

        if document["document_slug"] != latest["document_slug"]:
            return VersionDecision(
                action="",
                status="error",
                error_code="DOCUMENT_NAME_MISMATCH",
                error_message=(
                    "Cùng mã tài liệu phải giữ nguyên tên giữa các version"
                ),
            )

        return VersionDecision(action="NEW_VERSION")

    def evaluate_source(self, source_id: int):
        documents = self.repository.list_documents_for_version_check(source_id)
        result = {
            "source_id": source_id,
            "checked": 0,
            "ready": 0,
            "skipped": 0,
            "failed": 0,
            "items": [],
        }

        for document in documents:
            result["checked"] += 1
            checksum = ""
            try:
                data = self.drive.download_file(
                    document["drive_file_id"],
                    max_bytes=self.max_file_bytes,
                )
                checksum = hashlib.sha256(data).hexdigest()
                completed = self.repository.list_completed_versions(
                    source_id,
                    document["document_code"],
                )
                decision = self.decide(document, completed, checksum)
            except GoogleDriveError as exc:
                decision = VersionDecision(
                    action="",
                    status="error",
                    error_code="DOWNLOAD_FAILED",
                    error_message=str(exc),
                )

            row = self.repository.save_version_decision(
                drive_file_id=document["drive_file_id"],
                content_sha256=checksum,
                sync_action=decision.action,
                status=decision.status,
                error_code=decision.error_code,
                error_message=decision.error_message,
            )

            if row["status"] == "error":
                result["failed"] += 1
            elif row["status"] == "skipped":
                result["skipped"] += 1
            else:
                result["ready"] += 1

            result["items"].append(
                {
                    "drive_file_id": row["drive_file_id"],
                    "file_name": row["original_file_name"],
                    "document_code": row["document_code"],
                    "version": row["document_version"],
                    "status": row["status"],
                    "sync_action": row["sync_action"],
                    "error_code": row["error_code"],
                }
            )

        return result
