from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.product_sync.execute_sync import write_enabled
from app.product_sync.inventory_repository import InventorySyncRepository


class InventorySyncSettingsUpdate(BaseModel):
    enabled: bool
    interval_hours: int = Field(default=6, ge=1, le=168)


def create_inventory_sync_router(admin_dependency):
    router = APIRouter(
        prefix="/api/v1/products/inventory-sync",
        tags=["Inventory Sync"],
        dependencies=[Depends(admin_dependency)],
    )

    def current_repo(request: Request):
        repo = getattr(request.app.state, "inventory_sync", None)
        if repo is None:
            raise HTTPException(503, "Inventory Sync chưa được cấu hình")
        return repo

    @router.get("/status")
    def status(repo: InventorySyncRepository = Depends(current_repo)):
        return {**repo.get_status(), "write_enabled": write_enabled()}

    @router.get("/settings")
    def settings(repo: InventorySyncRepository = Depends(current_repo)):
        return repo.get_status()

    @router.put("/settings")
    def update_settings(
        payload: InventorySyncSettingsUpdate,
        repo: InventorySyncRepository = Depends(current_repo),
    ):
        return repo.update_settings(
            enabled=payload.enabled,
            interval_hours=payload.interval_hours,
        )

    @router.post("/run")
    def run_now(repo: InventorySyncRepository = Depends(current_repo)):
        if not write_enabled():
            raise HTTPException(409, "PRODUCT_SYNC_WRITE_ENABLED=false. Worker sẽ không ghi Qdrant.")
        current = repo.get_status()
        if current and current.get("running"):
            raise HTTPException(409, "Inventory Sync đang chạy.")
        return repo.request_run()

    return router
