from __future__ import annotations

from datetime import time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator


SyncMode = Literal["existing", "all_active"]


class ProductSyncRequest(BaseModel):
    mode: SyncMode = "existing"
    # Safe default for API clients. The Product UI explicitly sends false
    # when the admin requests a real sync.
    dry_run: bool = True


class ProductSyncSettingsUpdate(BaseModel):
    enabled: bool = False
    sync_mode: SyncMode = "existing"
    sync_time: time = time(hour=2, minute=0)
    timezone: str = Field(default="Asia/Ho_Chi_Minh", min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        value = value.strip()
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Múi giờ không hợp lệ") from exc
        return value
