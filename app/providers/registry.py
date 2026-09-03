from __future__ import annotations

from app.config import Settings
from app.providers.antigravity import AntigravityProvider
from app.providers.base import UsageProvider
from app.providers.claude import ClaudeProvider
from app.providers.codex import CodexProvider
from app.providers.deepseek import DeepSeekProvider
from app.providers.supergrok import SuperGrokProvider


def build_providers(settings: Settings) -> list[UsageProvider]:
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
        ClaudeProvider(settings.claude_credentials, timeout=timeout),
    ]
