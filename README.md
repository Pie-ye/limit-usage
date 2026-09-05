# limit-usage

Self-hosted dashboard for AI provider quotas:

| Provider | What you see |
|----------|----------------|
| **Codex** | 5-hour + weekly remaining % and reset countdown |
| **SuperGrok** | Weekly / billing pool % and reset (best-effort) |
| **DeepSeek** | API balance (total / granted / topped-up) |
| **Claude** | 5-hour + weekly used % and reset, via [claude-monitor](#claude-quota-sources) |

Default URL: **http://localhost:50048**

## Quick start (manual)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env: DEEPSEEK_API_KEY, auth paths if needed
python -m uvicorn app.main:app --host 0.0.0.0 --port 50048
```

Open http://localhost:50048

## Production run (Docker — preferred)

```bash
# stop legacy user unit if still present
systemctl --user disable --now limit-usage.service 2>/dev/null || true

docker compose up --build -d
docker compose ps
curl -sS http://127.0.0.1:50048/api/health
```

- Listens on **127.0.0.1:50048** (homepage widgets / edge proxy unchanged).
- Mounts `./data`, `~/.codex/auth.json` (ro), `~/.grok/auth.json` (rw for token rotation).
- Mounts host D-Bus + `apparmor:unconfined` so `/api/host/ups` can talk to host upowerd.
- `restart: unless-stopped`; ensure `docker` is enabled on boot.

### Legacy systemd user unit

```bash
./deploy/install-user-service.sh   # only if not using Docker
systemctl --user status limit-usage
```

## Configuration

See `.env.example`.

| Variable | Default | Meaning |
|----------|---------|---------|
| `PORT` | `50048` | HTTP port |
| `POLL_INTERVAL_SECONDS` | `60` | Background poll interval |
| `CODEX_AUTH_PATH` | `~/.codex/auth.json` | ChatGPT OAuth from `codex login` |
| `GROK_AUTH_PATH` | `~/.grok/auth.json` | From `grok login` |
| `SUPERGROK_COOKIE` | — | Optional grok.com session cookie |
| `DEEPSEEK_API_KEY` | — | DeepSeek API key |
| `CLAUDE_STATUSLINE_CAPTURE_PATH` | `~/.claude-monitor/statusline/latest.json` | claude-monitor statusline capture |
| `CLAUDE_USAGE_CACHE_PATH` | `~/.claude-monitor/state/claude-code-usage.json` | Claude Code usage cache copy |
| `CLAUDE_OFFICIAL_MAX_AGE_SECONDS` | `21600` | Ignore a local file past this age |
| `CLAUDE_OAUTH_MIN_INTERVAL_SECONDS` | `1800` | Spacing of OAuth fallback calls |
| `CLAUDE_CREDENTIALS_PATH` | `~/.claude/.credentials.json` | Claude OAuth fallback + tier hint |
| `DATABASE_PATH` | `./data/usage.db` | SQLite path |

## Claude quota sources

Anthropic's `api/oauth/usage` endpoint is rate limited **per account**, and
every running Claude Code session already polls it for its own footer. A
background poller on top of that earns a `429` with `Retry-After: 3600`, which
parks the Claude card for an hour at a time. So the card prefers two local
files Claude Code tooling already writes, and only calls the OAuth endpoint as
a last resort.

**Source A — statusline capture.** Claude Code hands every status line script
an official `rate_limits` block on stdin (`five_hour` / `seven_day` /
`spend_limit`, and on newer builds `model_scoped`). The
[claude-monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor)
(MIT) `--statusline` hook captures that block verbatim into
`~/.claude-monitor/statusline/latest.json`. `ClaudeProvider` reads this file
directly — it does **not** go through claude-monitor's `--write-state`, which
downgrades the numbers to a token-count `local_estimate` after 600 seconds.
The catch: this file only updates while an **interactive TUI** session has
activity. Headless / SDK / ACP sessions never render a status line, so they
never write it.

**Source B — Claude Code's own usage cache.** Claude Code also caches the
usage endpoint's response in `~/.claude.json` under `cachedUsageUtilization`
(this one includes the Fable `weekly_scoped` row). It refetches at most every
5 minutes, and only when the TUI starts or a dialog opens.
`deploy/claude-usage-cache-sync.py`, run by a systemd user timer every 2
minutes, copies that key out into
`~/.claude-monitor/state/claude-code-usage.json`.

**Merge rule.** Both sources are eligible as long as they're newer than
`CLAUDE_OFFICIAL_MAX_AGE_SECONDS`; for each window (5h / weekly / Fable) the
provider takes whichever eligible source is newer. The card's `source` field
reports which one(s) contributed: `statusline`, `claude-code-cache`, or
`statusline+claude-code-cache`. If the data backing the card is older than 15
minutes, the card's `message` says so in Chinese (官方額度資料為 N 分鐘前).

**Rollover.** Once a window's `resets_at` has passed, the card shows 0% for
that window instead of disappearing: the 5-hour window just stops showing a
reset time, and the weekly window's reset is pushed forward by 7 days.

**Source C — OAuth fallback.** Only when neither local source has a usable
5-hour or weekly window does the provider call the OAuth endpoint, at most
once every `CLAUDE_OAUTH_MIN_INTERVAL_SECONDS`. If Anthropic responds `429`,
the poller fully honours the `Retry-After` header before trying again. When
this path serves the card, `source` is `oauth/usage`.

### Gotcha: never bind-mount the credentials file

`docker-compose.yml` mounts the **directory** `~/.claude`, not
`~/.claude/.credentials.json`. Claude Code refreshes the OAuth token roughly
every 8 hours by writing a new file and renaming it into place. A single-file
bind mount resolves to an inode at container start and never follows that
rename, so the container keeps reading the pre-rotation token. Everything looks
fine until the old token is revoked, and then the card dies with:

```
Auth failed (401). ... "OAuth access token has been revoked."
```

which no restart of the *poller* fixes — only recreating the container did,
until the next rotation. Diagnose by comparing inodes:

```bash
stat -c 'inode=%i mtime=%y' ~/.claude/.credentials.json
docker exec limit-usage stat -c 'inode=%i mtime=%y' /secrets/claude/.credentials.json
```

Different inodes means the mount has gone stale. The same trap applies to any
other credential file a host tool rotates — `~/.claude.json` included, which is
exactly why source B is copied out by a sync script instead of being mounted
directly.

Setup:

```bash
uv tool install claude-monitor

# 1. status line hook — add to ~/.claude/settings.json
#    "statusLine": { "type": "command",
#                    "command": "~/.local/bin/claude-monitor --statusline" }

# 2. usage cache sync timer
install -m644 deploy/limit-usage-claude-monitor.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now limit-usage-claude-monitor.timer
systemctl --user restart limit-usage-claude-monitor.service   # first copy now
```

Check which source served the card:

```bash
curl -sS http://127.0.0.1:50048/api/usage | jq '.snapshots[] | select(.provider=="claude") | .source'
# "statusline"                     → statusline capture only
# "claude-code-cache"              → Claude Code's usage cache only
# "statusline+claude-code-cache"   → both contributed, newer wins per window
# "oauth/usage"                    → fell back; .message says why
```

## API

- `GET /api/health`
- `GET /api/usage` — all latest snapshots
- `GET /api/usage/{codex\|supergrok\|deepseek}`
- `GET /api/homepage` — flat fields for gethomepage customapi (Codex/Grok weekly %, DeepSeek CNY)
- `GET /api/host/ups` — UPS percent/state via upower (host-local)
- `POST /api/refresh` — force poll (rate-limited)
- `GET /api/history?provider=&limit=`
- `GET /api/trends?days=7` — 7-day series + burn-rate work estimates
- `GET /api/system/health` — companion endpoint for Homepage system health and backup freshness

## System Health and Backup Status Truth

The `/api/system/health` endpoint reports system health and backup freshness based on three independent sources of truth:

| Fact | Truth source | Freshness meaning |
|---|---|---|
| Local rsync | latest valid snapshot directory name | snapshot created within 26 hours |
| Offsite schedule | scripts/three_host/deployment-status.json | acceptance-gated means disabled, not failed or healthy |
| Health verification | latest health-*.json filename | checks executed within 26 hours |

State explicitly: `restic-repository: ok` means the repository was readable at report time, not that a current daily snapshot exists.

In `scripts/three_host/deployment-status.json`, schedule state is recorded as `acceptance-gated`, mapped to `restic_status: "disabled"` and `restic_display: "尚未啟用"`.

### Read-only Mount and Deployment Manifest

The deployment status manifest is mounted into the container via a single-file read-only mount:
`${HOME}/Container/scripts/three_host/deployment-status.json:/secrets/three-host-deployment-status.json:ro`

### API Fields and Aliases

- Independent fields: `rsync_status`, `rsync_display`, `rsync_stale`, `restic_status`, `restic_display`, `verification_status`, `verification_display`, `verification_updated_at`.
- Backward aliases: `stale` (tracks `verification_stale`), `freshness_display` (tracks `verification_display`), `updated_at` (tracks `verification_updated_at`), `expires_at`, `age_seconds`.

### Future Enablement Gate

Enabling the timers requires all of the following in one coordinated change:
`/home/pieye/Data` map-or-waive, strict full backup/check/isolated restore acceptance, installed/repository unit alignment, new snapshot-recency evidence, manifest/schema update, and `systemctl --user is-enabled` parity verification.

Explicitly note: this task does not run or enable either timer (`three-host-offsite-backup.timer` and `three-host-health.timer` remain disabled and inactive).

## Dashboard features

- **Urgency styling (bold red)**:
  - **Weekly only** (not 5h): remaining ≤20% / ≤10%, or reset within 2h / 30m
  - **DeepSeek CNY only**: warn when balance &lt; **10 CNY** (no reset; USD hidden)
- **7-day charts**: remaining % or CNY balance over time (Chart.js)
- **Work estimates**: light / medium / heavy task counts from recent burn rate (heuristic)

## Notes

- **Codex** uses `GET https://chatgpt.com/backend-api/wham/usage`. Windows are classified by `limit_window_seconds` (~18000 = 5h, ~604800 = 1w).
- **DeepSeek** uses official `GET /user/balance`. No weekly reset.
- **SuperGrok** uses `~/.grok/auth.json` OIDC tokens. Access tokens expire about every **6 hours**; the app auto-refreshes with `refresh_token` against `auth.x.ai` and writes rotated credentials back to `auth.json`. The Docker Compose mount for this file must remain writable (`:rw`) for refreshes to survive restarts. Billing data comes from grok.com gRPC-web (`GetGrokCreditsConfig`). If refresh fails, run `grok login` once.
- Snapshots are stored in SQLite so restarts still show the last successful data.
- Do not expose this service to the public internet without your own auth layer; tokens live on the host.

## Tests

```bash
pytest tests/ -q
```

## License

Private / personal use.
