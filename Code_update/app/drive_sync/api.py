from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.drive_sync.repository import GoogleDriveSyncRepository


class DriveSourceSettingsUpdate(BaseModel):
    enabled: bool


class DriveSyncSettingsUpdate(BaseModel):
    poll_minutes: int = Field(ge=1, le=1440)


def create_drive_sync_router(admin_dependency):
    router = APIRouter(
        prefix="/api/v1/drive-sync",
        tags=["Google Drive Sync"],
        dependencies=[Depends(admin_dependency)],
    )

    def current_repo(request: Request):
        repo = getattr(request.app.state, "drive_sync", None)
        if repo is None:
            raise HTTPException(503, "Google Drive Sync chưa được cấu hình")
        return repo

    @router.get("/overview")
    def overview(
        request: Request,
        repo: GoogleDriveSyncRepository = Depends(current_repo),
    ):
        settings = request.app.state.settings
        worker_poll_seconds = min(30, max(5, settings.google_drive_poll_seconds))
        threshold = max(120, worker_poll_seconds * 2 + 30)
        sources = repo.dashboard_sources()
        return {
            "enabled": settings.google_drive_sync_enabled,
            "configured_folder_count": len(
                settings.google_drive_department_folders
            ),
            "worker": repo.worker_status(
                online_within_seconds=threshold,
            ),
            "settings": repo.get_sync_settings(),
            "sources": sources,
        }

    @router.put("/settings")
    def update_settings(
        payload: DriveSyncSettingsUpdate,
        repo: GoogleDriveSyncRepository = Depends(current_repo),
    ):
        return repo.update_sync_settings(poll_minutes=payload.poll_minutes)

    @router.patch("/sources/{source_id}")
    def update_source(
        source_id: int,
        payload: DriveSourceSettingsUpdate,
        repo: GoogleDriveSyncRepository = Depends(current_repo),
    ):
        row = repo.update_source_settings(
            source_id,
            enabled=payload.enabled,
        )
        if row is None:
            raise HTTPException(404, "Không tìm thấy nguồn Google Drive")
        return row

    @router.post("/sources/{source_id}/run")
    def enqueue_source(
        source_id: int,
        repo: GoogleDriveSyncRepository = Depends(current_repo),
    ):
        source = repo.get_source(source_id)
        if source is None:
            raise HTTPException(404, "Không tìm thấy nguồn Google Drive")
        if not source["enabled"]:
            raise HTTPException(409, "Nguồn Google Drive đang tạm dừng")

        active_before = repo.enqueue_job(source_id, trigger_type="manual")
        if active_before is None:
            raise HTTPException(409, "Không thể tạo job đồng bộ")
        return active_before

    @router.get("/jobs")
    def jobs(
        page: int = Query(1, ge=1),
        page_size: int = Query(10, ge=1, le=100),
        source_id: int | None = Query(None, ge=1),
        status: str | None = Query(None),
        repo: GoogleDriveSyncRepository = Depends(current_repo),
    ):
        allowed = {"pending", "running", "completed", "partial", "failed"}
        if status and status not in allowed:
            raise HTTPException(422, "Trạng thái job không hợp lệ")
        return repo.jobs_page(
            page=page,
            page_size=page_size,
            source_id=source_id,
            status=status,
        )

    @router.get("/jobs/{job_id}")
    def job_detail(
        job_id: int,
        repo: GoogleDriveSyncRepository = Depends(current_repo),
    ):
        row = repo.get_job(job_id)
        if row is None:
            raise HTTPException(404, "Không tìm thấy job Google Drive")
        return row

    @router.get("/jobs/{job_id}/documents")
    def job_documents(
        job_id: int,
        page: int = Query(1, ge=1),
        page_size: int = Query(10, ge=1, le=100),
        repo: GoogleDriveSyncRepository = Depends(current_repo),
    ):
        if repo.get_job(job_id) is None:
            raise HTTPException(404, "Không tìm thấy job Google Drive")
        return repo.job_documents_page(
            job_id,
            page=page,
            page_size=page_size,
        )

    return router
