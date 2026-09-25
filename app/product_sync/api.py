from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.product_sync.delta_state_repository import ProductDeltaStateRepository
from app.product_sync.execute_sync import write_enabled
from app.product_sync.models import ProductSyncRequest, ProductSyncSettingsUpdate
from app.product_sync.repository import ProductSyncRepository


class DeltaCheckpointUpdate(BaseModel):
    last_success_at: datetime
    reason: str = Field(min_length=3, max_length=1000)
    confirmed: bool = False


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

    def delta_repo(request: Request):
        return ProductDeltaStateRepository(request.app.state.settings)

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

    @router.get("/checkpoint")
    def checkpoint_status(
        repo: ProductDeltaStateRepository = Depends(delta_repo),
    ):
        state = repo.checkpoint_status()
        if state is None:
            raise HTTPException(409, "Delta Sync chưa bootstrap")
        return state

    @router.put("/checkpoint")
    def update_checkpoint(
        payload: DeltaCheckpointUpdate,
        request: Request,
        repo: ProductSyncRepository = Depends(current_repo),
        delta: ProductDeltaStateRepository = Depends(delta_repo),
    ):
        if not payload.confirmed:
            raise HTTPException(400, "Bạn chưa xác nhận rủi ro điều chỉnh checkpoint")
        if payload.last_success_at.tzinfo is None or payload.last_success_at.utcoffset() is None:
            raise HTTPException(422, "Checkpoint phải kèm múi giờ")
        if payload.last_success_at.astimezone(UTC) > datetime.now(UTC):
            raise HTTPException(422, "Checkpoint không được nằm trong tương lai")

        active = repo.active_job()
        if active is not None:
            raise HTTPException(
                409,
                f"Không thể chỉnh checkpoint khi job #{active['id']} đang {active['status']}.",
            )

        inventory = getattr(request.app.state, "inventory_sync", None)
        inventory_state = inventory.get_status() if inventory is not None else None
        if inventory_state and (
            inventory_state.get("running") or inventory_state.get("run_requested")
        ):
            raise HTTPException(
                409,
                "Không thể chỉnh checkpoint khi Inventory Sync đang chạy hoặc chờ chạy.",
            )

        try:
            return delta.set_checkpoint(
                last_success_at=payload.last_success_at,
                reason=payload.reason,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

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
        page: int = Query(1, ge=1),
        page_size: int = Query(5, ge=1, le=100),
        limit: int | None = Query(None, ge=1, le=100),
        repo: ProductSyncRepository = Depends(current_repo),
    ):
        return repo.history_page(page=page, page_size=limit or page_size)

    @router.get("/history/{job_id}/changes")
    def sync_changes(
        job_id: int,
        page: int = Query(1, ge=1),
        page_size: int = Query(10, ge=1, le=100),
        repo: ProductSyncRepository = Depends(current_repo),
    ):
        return repo.product_changes_page(
            job_id,
            page=page,
            page_size=page_size,
        )

    return router
