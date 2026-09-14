from __future__ import annotations

from typing import Any

from app.config import Settings
from app.providers.antigravity import AntigravityProvider
from app.providers.base import UsageProvider
from app.providers.claude import ClaudeProvider
from app.providers.codex import CodexProvider
from app.providers.deepseek import DeepSeekProvider
from app.providers.supergrok import SuperGrokProvider
from app.services.claude_activity import ClaudeActivityProbe


def build_providers(
    settings: Settings,
    *,
    remote_claude: Any | None = None,
) -> list[UsageProvider]:
    timeout = settings.http_timeout_seconds
    return [
        CodexProvider(settings.codex_auth, timeout=timeout),
        SuperGrokProvider(
            settings.grok_auth,
            cookie=settings.supergrok_cookie,
            timeout=timeout,
        ),
        DeepSeekProvider(settings.deepseek_api_key, timeout=timeout),
        AntigravityProvider(settings.antigravity_token, timeout=timeout),
        ClaudeProvider(
            settings.claude_credentials,
            timeout=timeout,
            statusline_capture_path=settings.claude_statusline_capture,
            usage_cache_path=settings.claude_usage_cache,
            official_max_age_seconds=settings.claude_official_max_age_seconds,
            oauth_min_interval_seconds=settings.claude_oauth_min_interval_seconds,
            oauth_active_interval_seconds=settings.claude_oauth_active_interval_seconds,
            activity_probe=ClaudeActivityProbe(
                settings.claude_projects,
                active_window_seconds=settings.claude_activity_window_seconds,
            ),
            remote_readings=(remote_claude.entries if remote_claude else None),
        ),
    ]
