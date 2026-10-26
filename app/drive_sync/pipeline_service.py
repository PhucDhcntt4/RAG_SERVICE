from __future__ import annotations


class DrivePipelineError(RuntimeError):
    pass


class DriveSyncPipeline:
    def __init__(
        self,
        *,
        drive_repository,
        discovery_service,
        version_service,
        import_service,
        move_service,
    ):
        self.repository = drive_repository
        self.discovery = discovery_service
        self.version = version_service
        self.importer = import_service
        self.mover = move_service

    def run(self, folder_id: str, *, job_id: int | None = None, progress=None):
        source = self.repository.get_source_by_folder_id(folder_id)

        if source is None:
            raise DrivePipelineError(
                "Folder phòng ban chưa được đăng ký"
            )

        if not source["enabled"]:
            raise DrivePipelineError(
                "Nguồn Google Drive đang bị tạm dừng"
            )

        # Bước 1: tìm file trong PUBLIC/NEW và PRIVATE/NEW
        if progress is not None:
            progress("scan")
        scan_result = (
            self.discovery.scan_department(folder_id, job_id=job_id)
            if job_id is not None
            else self.discovery.scan_department(folder_id)
        )

        # Bước 2: tải file, tính SHA-256 và kiểm tra version
        if progress is not None:
            progress("version_check")
        version_result = self.version.evaluate_source(source["id"])

        # Bước 3: import tài liệu hợp lệ vào Qdrant
        if progress is not None:
            progress("import")
        import_result = self.importer.import_source(source["id"])

        # Bước 4: chuyển file sang DONE hoặc ERROR
        if progress is not None:
            progress("move")
        move_result = self.mover.move_source(source["id"])

        if progress is not None:
            progress("finalize")

        failed = (
            scan_result["invalid"]
            + version_result["failed"]
            + import_result["failed"]
            + move_result["failed"]
        )

        return {
            "source_id": source["id"],
            "department_code": source["department_code"],
            "status": "completed" if failed == 0 else "partial",
            "failed": failed,
            "scan": scan_result,
            "version_check": version_result,
            "import": import_result,
            "move": move_result,
        }
