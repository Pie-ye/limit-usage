# PRD: AI Usage Quota Dashboard

## Problem

Users need a long-running, self-hosted backend to monitor AI provider quotas (Codex 5h/1w, SuperGrok weekly, DeepSeek API balance) with live reset countdowns — without relying on desktop menu-bar tools.

## Goals

1. Query **Codex** 5-hour and weekly usage windows and reset times.
2. Query **SuperGrok** weekly (or billing-period) usage and reset times when credentials allow.
3. Query **DeepSeek** API balance (total / granted / topped-up).
4. Persist latest snapshots in SQLite so restarts still show last known quotas.
5. Serve a simple Web UI with progress bars and **live countdown** to each `resets_at`.
6. Listen on port **50048**.

## Non-Goals

- Telegram / Discord push notifications
- Multi-user auth / public multi-tenant SaaS
- Auto-redeeming Codex reset credits
- OpenAI Platform paid API usage scraping

## Constraints

- Python FastAPI + SQLite + simple HTML/JS UI
- Secrets via env / mounted files; never commit tokens
- SuperGrok uses best-effort non-public endpoints; must not break other providers
- Default poll interval ≥ 60s with backoff on failure

## Acceptance Criteria

- [ ] Backend runs long-term; after restart, last successful snapshots still visible
- [ ] Codex: 5h + 1w remaining % and `resets_at` countdown (real auth or mocks)
- [ ] DeepSeek: total / granted / topped_up + `is_available`
- [ ] SuperGrok: shows weekly usage + reset when auth works; clear setup error otherwise
- [ ] Dashboard shows live countdown and next poll time
- [ ] `.env.example` complete; secrets gitignored
- [ ] Service available at `http://localhost:50048`
- [ ] Unit tests cover snapshot parsing for Codex and DeepSeek
