"""Runtime configuration and vendor identity resolution for the routing policy service.

This module centralizes environment-based settings and external-to-internal vendor
namespace mappings. The service defaults strictly to binding to 127.0.0.1 so that
direct exposure across local network interfaces is impossible by default, reserving
external access exclusively for managed gateway or reverse proxy tunnels.

Vendor namespace mappings bridge external provider nomenclature (e.g. 'anthropic',
'openai', 'google', 'xai') to internal CLI pool identities ('claude', 'codex',
'agy', 'grok'). Unknown vendor names encountered in request payloads are safely
filtered out rather than triggering validation errors, ensuring resilient interop
with heterogeneous client versions.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# External provider aliases to internal CLI pool identifiers.
# Callers frequently send provider names ('anthropic', 'openai', 'google', 'xai'),
# whereas the policy engine operates on CLI pool prefixes ('claude', 'codex', 'agy', 'grok').
VENDOR_ALIASES: dict[str, str] = {
    "anthropic": "claude",
    "openai": "codex",
    "google": "agy",
    "xai": "grok",
}

# Recognized internal CLI pool vendors (pool ids in catalog/models.yaml).
INTERNAL_VENDORS: frozenset[str] = frozenset({"claude", "codex", "grok", "agy"})


def normalize_vendor(name: str) -> str | None:
    """Resolve an incoming vendor name to an internal CLI pool vendor identifier.

    If the name matches a known external alias, the internal name is returned.
    If it is already a recognized internal vendor, it passes through untouched.
    Unrecognized vendor names return None so callers can silently drop them.
    """
    cleaned = name.strip().lower()
    if cleaned in VENDOR_ALIASES:
        return VENDOR_ALIASES[cleaned]
    if cleaned in INTERNAL_VENDORS:
        return cleaned
    return None


def normalize_vendor_list(vendors: list[str] | None) -> list[str] | None:
    """Normalize a list of external or internal vendor names, filtering unknown items.

    Returns None if the input is None, indicating that no vendor constraint was requested.
    Deduplicates resolved names while preserving first-seen ordering.

    Design tradeoff note:
    When a client provides vendors but all of them are unknown to this service (e.g.
    `available_vendors: ["some-new-cli"]`), this returns `[]` rather than falling back
    to `None`. Returning `[]` causes the engine to evaluate zero eligible candidates and
    return `recommended: null` + `fallback_static`, signaling the client to honestly fall
    back to its own static ladder rather than silently dispatching to an unavailable vendor.
    """
    if vendors is None:
        return None
    normalized: list[str] = []
    for raw in vendors:
        resolved = normalize_vendor(raw)
        if resolved is not None and resolved not in normalized:
            normalized.append(resolved)
    return normalized


class Settings(BaseSettings):
    """Declarative settings loaded from environment variables with safe localhost defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 50100
    policy_dir: str = "./policy"
    limit_usage_routing_url: str = "http://127.0.0.1:50048/api/routing"
    signal_ttl_seconds: float = 30.0
    signal_timeout_seconds: float = 3.0
    recommend_ttl_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    """Return cached singleton instance of runtime settings."""
    return Settings()
