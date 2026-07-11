from __future__ import annotations

from typing import Any

import httpx

from app.models import AccountSnapshot, ProviderId, SnapshotStatus, UsageWindow, utcnow
from app.providers.base import error_snapshot, http_get_json, mask_secret

BALANCE_URL = "https://api.deepseek.com/user/balance"


def parse_balance_payload(data: dict[str, Any]) -> list[UsageWindow]:
    windows: list[UsageWindow] = []
    infos = data.get("balance_infos") or data.get("balanceInfos") or []
    if not isinstance(infos, list):
        infos = []

    # Prefer USD then CNY then first
    def sort_key(item: dict[str, Any]) -> int:
        cur = str(item.get("currency") or "").upper()
        if cur == "USD":
            return 0
        if cur == "CNY":
            return 1
        return 2

    infos_sorted = sorted(
        [i for i in infos if isinstance(i, dict)],
        key=sort_key,
    )

    for info in infos_sorted:
        currency = str(info.get("currency") or "")
        total = str(info.get("total_balance") or info.get("totalBalance") or "0")
        granted = str(info.get("granted_balance") or info.get("grantedBalance") or "0")
        topped = str(
            info.get("topped_up_balance") or info.get("toppedUpBalance") or "0"
        )
        key = f"balance-{currency.lower()}" if currency else "balance"
        windows.append(
            UsageWindow(
                key=key,
                label=f"Balance ({currency})" if currency else "Balance",
                used_percent=None,
                remaining_percent=None,
                resets_at=None,
                amount=total,
                currency=currency or None,
                raw_extra={
                    "total_balance": total,
                    "granted_balance": granted,
                    "topped_up_balance": topped,
                    "is_available": data.get("is_available"),
                },
            )
        )
    if not windows:
        windows.append(
            UsageWindow(
                key="balance",
                label="Balance",
                amount="0",
                raw_extra={"is_available": data.get("is_available")},
            )
        )
    return windows


class DeepSeekProvider:
    provider_id = ProviderId.DEEPSEEK
    display_name = "DeepSeek"

    def __init__(self, api_key: str | None, timeout: float = 20.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    async def fetch(self) -> AccountSnapshot:
        if not self.api_key:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.AUTH_ERROR,
                "DEEPSEEK_API_KEY not configured",
                source="config",
            )

        hint = mask_secret(self.api_key)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            status, data, err = await http_get_json(
                client,
                BALANCE_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                },
            )

        if status in (401, 403):
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.AUTH_ERROR,
                f"Invalid API key ({status})",
                account_hint=hint,
                source="user/balance",
            )
        if status == 429:
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.RATE_LIMITED,
                "DeepSeek rate limited",
                account_hint=hint,
                source="user/balance",
            )
        if not isinstance(data, dict):
            return error_snapshot(
                self.provider_id,
                self.display_name,
                SnapshotStatus.ERROR,
                err or f"Unexpected response (HTTP {status})",
                account_hint=hint,
                source="user/balance",
            )

        windows = parse_balance_payload(data)
        available = data.get("is_available")
        message = None
        if available is False:
            message = "Balance marked unavailable for API calls"
        return AccountSnapshot(
            provider=self.provider_id,
            display_name=self.display_name,
            account_hint=hint,
            status=SnapshotStatus.OK,
            message=message,
            windows=windows,
            fetched_at=utcnow(),
            source="user/balance",
        )
