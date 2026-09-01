from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def expand_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value).expanduser().resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 50048
    poll_interval_seconds: int = Field(default=60, ge=15)
    database_path: str = "./data/usage.db"

    codex_auth_path: str = "~/.codex/auth.json"
    grok_auth_path: str = "~/.grok/auth.json"
    supergrok_cookie: str | None = None
    deepseek_api_key: str | None = None
    antigravity_token_path: str = "~/.gemini/antigravity-acp/acp_token.json"

    http_timeout_seconds: float = 20.0
    max_backoff_seconds: int = 900
    refresh_min_interval_seconds: int = 10

    @property
    def db_path(self) -> Path:
        path = Path(self.database_path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        return path

    @property
    def codex_auth(self) -> Path:
        return expand_path(self.codex_auth_path) or Path.home() / ".codex" / "auth.json"

    @property
    def grok_auth(self) -> Path:
        return expand_path(self.grok_auth_path) or Path.home() / ".grok" / "auth.json"

    @property
    def antigravity_token(self) -> Path:
        return expand_path(self.antigravity_token_path) or Path.home() / ".gemini" / "antigravity-acp" / "acp_token.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
