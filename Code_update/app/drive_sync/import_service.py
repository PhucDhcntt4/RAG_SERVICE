from __future__ import annotations

import hashlib

from app.models import DocumentRequest


class DriveImportError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DriveImportService:
    def __init__(
        self,
        *,
        drive_client,
        drive_repository,
        taxonomy_repository,
        knowledge_service,
        max_file_bytes: int,
    ):
        self.drive = drive_client
        self.repository = drive_repository
        self.taxonomy = taxonomy_repository
        self.knowledge = knowledge_service
        self.max_file_bytes = max_file_bytes

    def _qdrant_document_if_written(self, source_key: str):
        repository = self.knowledge.repository
        document_id = repository.document_id_for_source(source_key)
        row = repository.get_document(document_id)
        if row is None or row.get("source_key") != source_key:
            return None
        return int(document_id)

    def _restore_previous_version(self, new_qdrant_id, old_qdrant_ids):
        if new_qdrant_id is None:
            return True

        restored = True
        for old_id in old_qdrant_ids:
            try:
                restored = self.knowledge.set_active(old_id, True) is not None and restored
            except Exception:
                restored = False
        try:
            restored = (
                self.knowledge.set_active(new_qdrant_id, False) is not None
                and restored
            )
        except Exception:
            restored = False
        return restored

    @staticmethod
    def _safe_error(exc: Exception):
        if isinstance(exc, DriveImportError):
            return exc.code, str(exc)
        return type(exc).__name__.upper()[:100], "Không thể import tài liệu vào RAG"

    def import_document(self, document_id: int):
        document = self.repository.claim_document_for_import(document_id)
        if document is None:
            raise DriveImportError(
                "DOCUMENT_NOT_READY", "Tài liệu không sẵn sàng để import"
            )

        old_versions = self.repository.list_active_versions(
            document["source_id"],
            document["document_code"],
            exclude_id=document["id"],
        )
        old_qdrant_ids = [
            int(item["qdrant_document_id"])
            for item in old_versions
            if item["qdrant_document_id"] is not None
        ]
        new_qdrant_id = None

        try:
            data = self.drive.download_file(
                document["drive_file_id"], max_bytes=self.max_file_bytes
            )
            checksum = hashlib.sha256(data).hexdigest()
            if checksum != document["content_sha256"]:
                raise DriveImportError(
                    "FILE_CHANGED_AFTER_VERSION_CHECK",
                    "Nội dung file đã thay đổi sau khi kiểm tra version",
                )

            group = self.taxonomy.get_group(document["group_id"])
            if group is None or not group["is_active"]:
                raise DriveImportError(
                    "DOCUMENT_GROUP_UNAVAILABLE",
                    "Nhóm tài liệu không tồn tại hoặc đang bị tạm dừng",
                )

            text, prepared_chunks, prepared_assets = (
                self.knowledge.prepare_upload_with_assets(
                    document["original_file_name"], data
                )
            )
            payload = DocumentRequest(
                source_key=document["source_key"],
                title=document["document_slug"].replace("-", " "),
                category=group["code"],
                doc_type_id=group["doc_type_id"],
                group_id=group["id"],
                text=text,
            )
            result = self.knowledge.ingest(
                payload,
                original_data=data,
                original_filename=document["original_file_name"],
                prepared_chunks=prepared_chunks,
                prepared_assets=prepared_assets,
                initial_active=False,
            )
            new_qdrant_id = int(result["id"])
            metadata = self.knowledge.repository.set_document_metadata(
                new_qdrant_id,
                document_code=document["document_code"],
                document_version=document["document_version"],
                drive_scope=document["drive_scope"],
            )
            if metadata is None:
                raise DriveImportError(
                    "QDRANT_METADATA_FAILED",
                    "Không thể ghi metadata phiên bản vào Qdrant",
                )
            self.repository.save_qdrant_document_id(document["id"], new_qdrant_id)

            if self.knowledge.set_active(new_qdrant_id, True) is None:
                raise DriveImportError(
                    "QDRANT_ACTIVATION_FAILED", "Không bật được version mới"
                )
            for old_id in old_qdrant_ids:
                if self.knowledge.set_active(old_id, False) is None:
                    raise DriveImportError(
                        "QDRANT_DEACTIVATION_FAILED",
                        "Không tắt được version cũ",
                    )

            completed = self.repository.complete_import(document["id"])
            return {
                "document_id": completed["id"],
                "document_code": completed["document_code"],
                "version": completed["document_version"],
                "qdrant_document_id": new_qdrant_id,
                "status": completed["status"],
            }
        except Exception as exc:
            if new_qdrant_id is None:
                try:
                    new_qdrant_id = self._qdrant_document_if_written(
                        document["source_key"]
                    )
                except Exception:
                    new_qdrant_id = None

            code, message = self._safe_error(exc)
            if self._restore_previous_version(new_qdrant_id, old_qdrant_ids):
                self.repository.fail_import(
                    document["id"], error_code=code, error_message=message
                )
            else:
                self.repository.record_processing_issue(
                    document["id"],
                    error_code="COMPENSATION_FAILED",
                    error_message=(
                        "Không khôi phục được trạng thái Qdrant; "
                        "cần kiểm tra thủ công"
                    ),
                )
            raise

    def import_source(self, source_id: int):
        documents = self.repository.list_documents_ready_for_import(source_id)
        result = {
            "source_id": source_id,
            "total": len(documents),
            "completed": 0,
            "failed": 0,
            "items": [],
        }
        for document in documents:
            try:
                item = self.import_document(document["id"])
                result["completed"] += 1
            except Exception as exc:
                item = {
                    "document_id": document["id"],
                    "document_code": document["document_code"],
                    "version": document["document_version"],
                    "status": "error",
                    "error_type": type(exc).__name__,
                }
                result["failed"] += 1
            result["items"].append(item)
        return result
