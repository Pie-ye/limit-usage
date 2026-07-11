# Design: AI Usage Quota Dashboard

## Architecture

```
Providers (Codex / SuperGrok / DeepSeek)
        │ fetch → AccountSnapshot
        ▼
Background Poller (asyncio) ──► SQLite (latest + history)
        │
   REST + HTML dashboard :50048
```

## Unified model

- `UsageWindow`: key, label, used/remaining %, `resets_at`, `limit_window_seconds`, raw_extra
- `AccountSnapshot`: provider, display_name, account_hint, status, message, windows, fetched_at, next_poll_at

Statuses: `ok | auth_error | rate_limited | unsupported | error`

## Providers

### Codex

- Auth: `CODEX_AUTH_PATH` or `~/.codex/auth.json`
- `GET https://chatgpt.com/backend-api/wham/usage` with Bearer access_token
- Map windows by `limit_window_seconds` (~18000 → 5h, ~604800 → 1w)
- Best-effort token refresh if refresh_token present

### DeepSeek

- `GET https://api.deepseek.com/user/balance` with API key
- Single `balance` window; no period reset

### SuperGrok

Fallback order:

1. Read `GROK_AUTH_PATH` / `~/.grok/auth.json`
2. Optional bearer/cookie HTTP probe to grok.com billing path
3. Explicit `auth_error` / `unsupported` without failing other providers

## Persistence

SQLite tables:

- `latest_snapshots(provider PK, payload JSON, fetched_at, status)`
- `usage_history(id, provider, payload JSON, fetched_at)`

## Poller

- Interval: `POLL_INTERVAL_SECONDS` (default 60)
- On failure: exponential backoff up to 15m
- Manual `POST /api/refresh` triggers immediate cycle (rate-limited)

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | Health |
| GET | `/api/usage` | All latest snapshots |
| GET | `/api/usage/{provider}` | One provider |
| POST | `/api/refresh` | Force poll |
| GET | `/api/history` | Optional history |
| GET | `/` | Dashboard |

## Security

- Env-only secrets; mount auth files in Docker
- No multi-tenant auth in MVP; bind 0.0.0.0:50048 for LAN/self-host

## Tradeoffs

- SuperGrok reverse-engineered endpoints may break → isolated provider module
- Simple HTML over SPA → lower maintenance for a personal dashboard
