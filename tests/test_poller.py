from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db.repository import Repository
from app.models import AccountSnapshot, ProviderId, SnapshotStatus, UsageWindow, utcnow
from app.services.poller import UsagePoller


class FakeProvider:
    def __init__(
        self,
        provider_id: ProviderId,
        responses: list[AccountSnapshot],
        *,
        min_interval_seconds: int = 0,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.display_name = provider_id.value
        self.min_interval_seconds = min_interval_seconds
        self.retry_after_seconds = retry_after_seconds
        self._responses = list(responses)
        self.calls = 0

    async def fetch(self) -> AccountSnapshot:
        self.calls += 1
        if not self._responses:
            raise AssertionError(f"{self.provider_id} fetched more times than planned")
        return self._responses.pop(0)


def _ok(provider: ProviderId, remaining: float, fetched_at: datetime | None = None) -> AccountSnapshot:
    return AccountSnapshot(
        provider=provider,
        display_name=provider.value,
        status=SnapshotStatus.OK,
        windows=[
            UsageWindow(
                key="1w",
                label="Weekly",
                used_percent=100.0 - remaining,
                remaining_percent=remaining,
                limit_window_seconds=604800,
            )
        ],
        fetched_at=fetched_at or utcnow(),
        source="test",
    )


def _limited(provider: ProviderId) -> AccountSnapshot:
    return AccountSnapshot(
        provider=provider,
        display_name=provider.value,
        status=SnapshotStatus.RATE_LIMITED,
        message="rate limited",
        windows=[],
        fetched_at=utcnow(),
        source="test",
    )


@pytest.mark.asyncio
async def test_poller_does_not_globally_backoff_on_one_provider_429(tmp_path: Path):
    repo = Repository(tmp_path / "usage.db")
    claude = FakeProvider(ProviderId.CLAUDE, [_limited(ProviderId.CLAUDE)])
    codex = FakeProvider(ProviderId.CODEX, [_ok(ProviderId.CODEX, 80.0)])
    poller = UsagePoller(
        [claude, codex],
        repo,
        interval_seconds=60,
        max_backoff_seconds=900,
    )

    snaps = await poller.poll_once()
    by_id = {s.provider: s for s in snaps}
    assert by_id[ProviderId.CODEX].status == SnapshotStatus.OK
    assert poller._consecutive_failures == 0
    assert poller._current_delay() == 60.0
    assert ProviderId.CLAUDE.value in poller._next_fetch_at


@pytest.mark.asyncio
async def test_poller_keeps_last_good_windows_on_429(tmp_path: Path):
    repo = Repository(tmp_path / "usage.db")
    first_ok = _ok(
        ProviderId.CLAUDE,
        91.0,
        fetched_at=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
    )
    claude = FakeProvider(ProviderId.CLAUDE, [first_ok, _limited(ProviderId.CLAUDE)])
    poller = UsagePoller([claude], repo, interval_seconds=60)

    first = await poller.poll_once()
    assert first[0].status == SnapshotStatus.OK
    assert first[0].windows[0].remaining_percent == 91.0

    # Allow the per-provider skip to be bypassed so we actually re-fetch.
    second = await poller.poll_once(ignore_skip=True)
    assert claude.calls == 2
    assert second[0].status == SnapshotStatus.OK
    assert second[0].windows[0].remaining_percent == 91.0
    assert second[0].message and "沿用上次額度" in second[0].message
    assert "rate limited" in second[0].message
    assert second[0].fetched_at == first_ok.fetched_at

    stored = repo.get_latest(ProviderId.CLAUDE)
    assert stored is not None
    assert stored.windows[0].remaining_percent == 91.0
    assert stored.status == SnapshotStatus.OK


@pytest.mark.asyncio
async def test_poller_skips_provider_until_backoff_elapses(tmp_path: Path):
    repo = Repository(tmp_path / "usage.db")
    claude = FakeProvider(
        ProviderId.CLAUDE,
        [_limited(ProviderId.CLAUDE), _ok(ProviderId.CLAUDE, 50.0)],
        min_interval_seconds=300,
        retry_after_seconds=120,
    )
    poller = UsagePoller([claude], repo, interval_seconds=60)

    await poller.poll_once()
    assert claude.calls == 1
    skip_until = poller._next_fetch_at[ProviderId.CLAUDE.value]
    assert skip_until - utcnow() > timedelta(seconds=200)

    await poller.poll_once()
    assert claude.calls == 1  # skipped; still in backoff


@pytest.mark.asyncio
async def test_poller_honours_retry_after_beyond_backoff_cap(tmp_path: Path):
    repo = Repository(tmp_path / "usage.db")
    claude = FakeProvider(
        ProviderId.CLAUDE,
        [_limited(ProviderId.CLAUDE)],
        retry_after_seconds=3600,
    )
    codex = FakeProvider(ProviderId.CODEX, [_ok(ProviderId.CODEX, 80.0)])
    poller = UsagePoller(
        [claude, codex],
        repo,
        interval_seconds=60,
        max_backoff_seconds=900,
    )

    start = utcnow()
    await poller.poll_once()

    assert poller._next_fetch_at[ProviderId.CLAUDE.value] >= start + timedelta(seconds=3600)
    assert ProviderId.CODEX.value not in poller._next_fetch_at


@pytest.mark.asyncio
async def test_poller_backoff_cap_still_applies_without_retry_after(tmp_path: Path):
    repo = Repository(tmp_path / "usage.db")
    claude = FakeProvider(
        ProviderId.CLAUDE,
        [_limited(ProviderId.CLAUDE) for _ in range(6)],
        retry_after_seconds=None,
    )
    poller = UsagePoller(
        [claude],
        repo,
        interval_seconds=60,
        max_backoff_seconds=900,
    )

    for _ in range(6):
        poll_time = utcnow()
        await poller.poll_once(ignore_skip=True)

    skip_until = poller._next_fetch_at[ProviderId.CLAUDE.value]
    assert (skip_until - poll_time) <= timedelta(seconds=900, milliseconds=100)
