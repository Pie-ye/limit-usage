from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Sequence

from app.db.repository import Repository
from app.models import AccountSnapshot, ProviderId, utcnow
from app.providers.base import UsageProvider

logger = logging.getLogger(__name__)


class UsagePoller:
    def __init__(
        self,
        providers: Sequence[UsageProvider],
        repository: Repository,
        *,
        interval_seconds: int = 60,
        max_backoff_seconds: int = 900,
        refresh_min_interval_seconds: int = 10,
    ) -> None:
        self.providers = list(providers)
        self.repository = repository
        self.interval_seconds = interval_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.refresh_min_interval_seconds = refresh_min_interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._consecutive_failures = 0
        self._last_refresh_at: datetime | None = None
        self._next_poll_at: datetime | None = None

    @property
    def next_poll_at(self) -> datetime | None:
        return self._next_poll_at

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name="usage-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            await self.poll_once()
            delay = self._current_delay()
            self._next_poll_at = utcnow() + timedelta(seconds=delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue

    def _current_delay(self) -> float:
        if self._consecutive_failures <= 0:
            return float(self.interval_seconds)
        # 60 → 120 → 300 → ... cap
        factor = min(self._consecutive_failures, 6)
        delay = self.interval_seconds * (2 ** (factor - 1))
        return float(min(delay, self.max_backoff_seconds))

    async def poll_once(self) -> list[AccountSnapshot]:
        async with self._lock:
            return await self._poll_unlocked()

    async def force_refresh(self) -> tuple[list[AccountSnapshot], str | None]:
        now = utcnow()
        if self._last_refresh_at:
            elapsed = (now - self._last_refresh_at).total_seconds()
            if elapsed < self.refresh_min_interval_seconds:
                wait = self.refresh_min_interval_seconds - elapsed
                return (
                    self.repository.get_all_latest(),
                    f"Refresh rate-limited; try again in {wait:.0f}s",
                )
        snapshots = await self.poll_once()
        return snapshots, None

    async def _poll_unlocked(self) -> list[AccountSnapshot]:
        results: list[AccountSnapshot] = []
        any_hard_failure = False
        next_at = utcnow() + timedelta(seconds=self._current_delay())

        for provider in self.providers:
            try:
                snapshot = await provider.fetch()
            except Exception as exc:
                logger.exception("Provider %s failed", provider.provider_id)
                from app.models import SnapshotStatus
                from app.providers.base import error_snapshot

                snapshot = error_snapshot(
                    provider.provider_id,
                    getattr(provider, "display_name", provider.provider_id.value),
                    SnapshotStatus.ERROR,
                    f"Unhandled error: {exc}",
                )
                any_hard_failure = True

            snapshot = snapshot.with_next_poll(next_at)
            self.repository.save_snapshot(snapshot)
            results.append(snapshot)
            if snapshot.status.value in {"error", "rate_limited"}:
                any_hard_failure = True

        if any_hard_failure:
            self._consecutive_failures += 1
        else:
            self._consecutive_failures = 0

        self._last_refresh_at = utcnow()
        self._next_poll_at = utcnow() + timedelta(seconds=self._current_delay())
        # Update next_poll on stored snapshots
        for snap in results:
            updated = snap.with_next_poll(self._next_poll_at)
            self.repository.save_snapshot(updated, keep_history=False)
        return [s.with_next_poll(self._next_poll_at) for s in results]

    def get_snapshots(self) -> list[AccountSnapshot]:
        stored = {s.provider: s for s in self.repository.get_all_latest()}
        # Ensure all known providers appear (placeholder if never polled)
        ordered: list[AccountSnapshot] = []
        for provider in self.providers:
            pid = provider.provider_id
            if pid in stored:
                snap = stored[pid]
                if self._next_poll_at and not snap.next_poll_at:
                    snap = snap.with_next_poll(self._next_poll_at)
                ordered.append(snap)
            else:
                from app.models import SnapshotStatus

                ordered.append(
                    AccountSnapshot(
                        provider=pid,
                        display_name=getattr(provider, "display_name", pid.value),
                        status=SnapshotStatus.UNSUPPORTED,
                        message="Waiting for first poll…",
                        next_poll_at=self._next_poll_at,
                    )
                )
        return ordered
