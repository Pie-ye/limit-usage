from __future__ import annotations

import json
import sqlite3
from datetime import datetime  # noqa: TC003 — used at runtime in get_history
from pathlib import Path
from typing import Any

from app.models import AccountSnapshot, ProviderId


class Repository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS latest_snapshots (
                    provider TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_history_provider_fetched
                    ON usage_history(provider, fetched_at DESC);
                """
            )

    def save_snapshot(self, snapshot: AccountSnapshot, *, keep_history: bool = True) -> None:
        payload = snapshot.model_dump_json()
        fetched_at = snapshot.fetched_at.isoformat()
        status = snapshot.status.value
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO latest_snapshots(provider, payload, fetched_at, status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    payload=excluded.payload,
                    fetched_at=excluded.fetched_at,
                    status=excluded.status
                """,
                (snapshot.provider.value, payload, fetched_at, status),
            )
            if keep_history and snapshot.status.value == "ok":
                conn.execute(
                    """
                    INSERT INTO usage_history(provider, payload, fetched_at)
                    VALUES (?, ?, ?)
                    """,
                    (snapshot.provider.value, payload, fetched_at),
                )

    def get_all_latest(self) -> list[AccountSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM latest_snapshots ORDER BY provider"
            ).fetchall()
        return [AccountSnapshot.model_validate_json(row["payload"]) for row in rows]

    def get_latest(self, provider: ProviderId | str) -> AccountSnapshot | None:
        key = provider.value if isinstance(provider, ProviderId) else provider
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM latest_snapshots WHERE provider = ?",
                (key,),
            ).fetchone()
        if not row:
            return None
        return AccountSnapshot.model_validate_json(row["payload"])

    def get_history(
        self,
        provider: ProviderId | str | None = None,
        limit: int = 50,
        *,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 20000))
        with self._connect() as conn:
            if provider and since:
                key = provider.value if isinstance(provider, ProviderId) else provider
                rows = conn.execute(
                    """
                    SELECT provider, payload, fetched_at FROM usage_history
                    WHERE provider = ? AND fetched_at >= ?
                    ORDER BY id ASC LIMIT ?
                    """,
                    (key, since.isoformat(), limit),
                ).fetchall()
            elif provider:
                key = provider.value if isinstance(provider, ProviderId) else provider
                rows = conn.execute(
                    """
                    SELECT provider, payload, fetched_at FROM usage_history
                    WHERE provider = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (key, limit),
                ).fetchall()
                rows = list(reversed(rows))
            elif since:
                rows = conn.execute(
                    """
                    SELECT provider, payload, fetched_at FROM usage_history
                    WHERE fetched_at >= ?
                    ORDER BY id ASC LIMIT ?
                    """,
                    (since.isoformat(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT provider, payload, fetched_at FROM usage_history
                    ORDER BY id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                rows = list(reversed(rows))
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "provider": row["provider"],
                    "fetched_at": row["fetched_at"],
                    "snapshot": json.loads(row["payload"]),
                }
            )
        return result
