from __future__ import annotations

from typing import Protocol

import httpx

from app.models import AccountSnapshot, ProviderId, SnapshotStatus, utcnow


class UsageProvider(Protocol):
    provider_id: ProviderId
    display_name: str

    async def fetch(self) -> AccountSnapshot: ...


def error_snapshot(
    provider: ProviderId,
    display_name: str,
    status: SnapshotStatus,
    message: str,
    *,
    account_hint: str | None = None,
    source: str | None = None,
) -> AccountSnapshot:
    return AccountSnapshot(
        provider=provider,
        display_name=display_name,
        account_hint=account_hint,
        status=status,
        message=message,
        windows=[],
        fetched_at=utcnow(),
        source=source,
    )


def mask_secret(value: str, visible: int = 4) -> str:
    if len(value) <= visible * 2:
        return "***"
    return f"{value[:visible]}…{value[-visible:]}"


async def http_get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict | list | None, str | None]:
    try:
        resp = await client.get(url, headers=headers or {})
    except httpx.HTTPError as exc:
        return 0, None, f"HTTP request failed: {exc}"
    text = resp.text
    if resp.status_code == 429:
        return resp.status_code, None, "rate limited"
    if resp.status_code >= 400:
        return resp.status_code, None, text[:300] or resp.reason_phrase
    try:
        return resp.status_code, resp.json(), None
    except ValueError:
        return resp.status_code, None, "invalid JSON response"
