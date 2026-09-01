from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProviderId(str, Enum):
    CODEX = "codex"
    SUPERGROK = "supergrok"
    DEEPSEEK = "deepseek"
    ANTIGRAVITY = "antigravity"


class SnapshotStatus(str, Enum):
    OK = "ok"
    AUTH_ERROR = "auth_error"
    RATE_LIMITED = "rate_limited"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class UsageWindow(BaseModel):
    key: str
    label: str
    used_percent: float | None = None
    remaining_percent: float | None = None
    resets_at: datetime | None = None
    limit_window_seconds: int | None = None
    # Free-form metrics (e.g. DeepSeek balances)
    amount: str | None = None
    currency: str | None = None
    raw_extra: dict[str, Any] = Field(default_factory=dict)


class AccountSnapshot(BaseModel):
    provider: ProviderId
    display_name: str
    account_hint: str | None = None
    status: SnapshotStatus
    message: str | None = None
    windows: list[UsageWindow] = Field(default_factory=list)
    fetched_at: datetime = Field(default_factory=utcnow)
    next_poll_at: datetime | None = None
    source: str | None = None

    def with_next_poll(self, next_poll_at: datetime | None) -> AccountSnapshot:
        return self.model_copy(update={"next_poll_at": next_poll_at})


class UsageResponse(BaseModel):
    snapshots: list[AccountSnapshot]
    server_time: datetime = Field(default_factory=utcnow)
    poll_interval_seconds: int


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    server_time: datetime = Field(default_factory=utcnow)
