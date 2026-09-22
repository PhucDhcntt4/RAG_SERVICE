from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.product_sync.execute_sync import write_enabled
from app.product_sync.models import ProductSyncRequest, ProductSyncSettingsUpdate
from app.product_sync.repository import ProductSyncRepository


def create_product_sync_router(admin_dependency):
    router = APIRouter(
        prefix="/api/v1/products/sync",
        tags=["Product Sync"],
        dependencies=[Depends(admin_dependency)],
    )

    def current_repo(request: Request):
        repo = getattr(request.app.state, "product_sync", None)
        if repo is None:
            raise HTTPException(503, "Product Sync chưa được cấu hình")
        return repo

    @router.get("/settings")
    def sync_settings(repo: ProductSyncRepository = Depends(current_repo)):
        return repo.get_settings()

    @router.put("/settings")
    def update_sync_settings(
        payload: ProductSyncSettingsUpdate,
        repo: ProductSyncRepository = Depends(current_repo),
    ):
        return repo.update_settings(
            enabled=payload.enabled,
            sync_mode=payload.sync_mode,
            sync_time=payload.sync_time,
            timezone=payload.timezone,
        )

    @router.post("")
    def enqueue_sync(
        payload: ProductSyncRequest,
        repo: ProductSyncRepository = Depends(current_repo),
    ):
        if not payload.dry_run and not write_enabled():
            raise HTTPException(
                409,
                "PRODUCT_SYNC_WRITE_ENABLED=false. Bật flag trong .env/Terminal worker trước khi sync thật.",
            )

        active = repo.active_job()
        if active is not None:
            raise HTTPException(
                409,
                f"Đang có sync job #{active['id']} ở trạng thái {active['status']}.",
            )

        row = repo.enqueue(
            mode=payload.mode,
            trigger_type="manual",
            dry_run=payload.dry_run,
        )
        return row

    @router.get("/status")
    def sync_status(repo: ProductSyncRepository = Depends(current_repo)):
        return {
            "job": repo.latest_job(),
            "active_job": repo.active_job(),
            "worker": repo.worker_status(),
            "write_enabled": write_enabled(),
        }

    @router.get("/history")
    def sync_history(
        limit: int = Query(10, ge=1, le=100),
        repo: ProductSyncRepository = Depends(current_repo),
    ):
        return {"jobs": repo.history(limit)}

    return router
