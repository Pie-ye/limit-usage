from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Sequence

from app.db.repository import Repository
from app.models import AccountSnapshot, ProviderId, SnapshotStatus, utcnow
from app.providers.base import UsageProvider

logger = logging.getLogger(__name__)

SOFT_FAILURES = {SnapshotStatus.RATE_LIMITED, SnapshotStatus.ERROR}
HARD_STATUS_VALUES = {"error", "rate_limited"}


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
        # Per-provider: do not let one 429 stall Codex/Grok/DeepSeek.
        self._next_fetch_at: dict[str, datetime] = {}
        self._provider_failures: dict[str, int] = {}

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

    async def poll_once(self, *, ignore_skip: bool = False) -> list[AccountSnapshot]:
        async with self._lock:
            return await self._poll_unlocked(ignore_skip=ignore_skip)

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
        snapshots = await self.poll_once(ignore_skip=True)
        return snapshots, None

    def _provider_key(self, provider: UsageProvider) -> str:
        pid = provider.provider_id
        return pid.value if isinstance(pid, ProviderId) else str(pid)

    def _min_interval(self, provider: UsageProvider) -> float:
        try:
            value = float(getattr(provider, "min_interval_seconds", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, value)

    def _retry_after(self, provider: UsageProvider) -> float:
        try:
            value = float(getattr(provider, "retry_after_seconds", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, value)

    def _schedule_next_fetch(
        self,
        provider: UsageProvider,
        snapshot: AccountSnapshot,
        *,
        now: datetime,
    ) -> None:
        key = self._provider_key(provider)
        min_interval = self._min_interval(provider)
        if snapshot.status.value in HARD_STATUS_VALUES:
            fails = self._provider_failures.get(key, 0) + 1
            self._provider_failures[key] = fails
            factor = min(fails, 6)
            delay = float(self.interval_seconds * (2 ** (factor - 1)))
            delay = max(delay, min_interval, self._retry_after(provider), float(self.interval_seconds))
            delay = min(delay, float(self.max_backoff_seconds))
            self._next_fetch_at[key] = now + timedelta(seconds=delay)
            logger.info(
                "Provider %s backing off for %.0fs (status=%s failures=%s)",
                key,
                delay,
                snapshot.status.value,
                fails,
            )
            return
        self._provider_failures[key] = 0
        if min_interval > 0:
            self._next_fetch_at[key] = now + timedelta(seconds=min_interval)
        else:
            self._next_fetch_at.pop(key, None)

    def _reuse_last_good(self, snapshot: AccountSnapshot) -> tuple[AccountSnapshot, bool]:
        """Keep last successful quota windows on transient 429/errors.

        Homepage widgets map used_percent fields; an empty rate_limited
        snapshot blanks those numbers and looks like a status drop.
        """
        if snapshot.status not in SOFT_FAILURES:
            return snapshot, False
        if snapshot.windows:
            return snapshot, False
        prev = self.repository.get_latest(snapshot.provider)
        if not prev or not prev.windows:
            return snapshot, False
        logger.warning(
            "Provider %s %s; keeping last good windows from %s",
            snapshot.provider.value,
            snapshot.status.value,
            prev.fetched_at.isoformat(),
        )
        detail = snapshot.message or snapshot.status.value
        return (
            prev.model_copy(
                update={
                    "status": SnapshotStatus.OK,
                    "message": f"沿用上次額度（{detail}）",
                    "account_hint": snapshot.account_hint or prev.account_hint,
                    "next_poll_at": snapshot.next_poll_at,
                }
            ),
            True,
        )

    async def _poll_unlocked(self, *, ignore_skip: bool = False) -> list[AccountSnapshot]:
        results: list[AccountSnapshot] = []
        fetched = 0
        fetched_hard = 0
        now = utcnow()
        next_at = now + timedelta(seconds=self._current_delay())

        for provider in self.providers:
            key = self._provider_key(provider)
            skip_until = None if ignore_skip else self._next_fetch_at.get(key)
            if skip_until is not None:
                if skip_until.tzinfo is None:
                    skip_until = skip_until.replace(tzinfo=timezone.utc)
                if now < skip_until:
                    prev = self.repository.get_latest(provider.provider_id)
                    if prev:
                        results.append(prev.with_next_poll(next_at))
                        continue

            try:
                snapshot = await provider.fetch()
            except Exception as exc:
                logger.exception("Provider %s failed", provider.provider_id)
                from app.providers.base import error_snapshot

                snapshot = error_snapshot(
                    provider.provider_id,
                    getattr(provider, "display_name", provider.provider_id.value),
                    SnapshotStatus.ERROR,
                    f"Unhandled error: {exc}",
                )

            fetched += 1
            raw_status = snapshot.status
            self._schedule_next_fetch(provider, snapshot, now=utcnow())
            snapshot, reused = self._reuse_last_good(snapshot)
            snapshot = snapshot.with_next_poll(next_at)
            self.repository.save_snapshot(snapshot, keep_history=not reused)
            results.append(snapshot)
            if raw_status.value in HARD_STATUS_VALUES:
                fetched_hard += 1

        # Global backoff only when every provider we actually fetched failed.
        # A single Claude 429 must not stall Codex/Grok/DeepSeek.
        if fetched and fetched_hard == fetched:
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
