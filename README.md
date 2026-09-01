# limit-usage

Self-hosted dashboard for AI provider quotas:

| Provider | What you see |
|----------|----------------|
| **Codex** | 5-hour + weekly remaining % and reset countdown |
| **SuperGrok** | Weekly / billing pool % and reset (best-effort) |
| **DeepSeek** | API balance (total / granted / topped-up) |

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
| `DATABASE_PATH` | `./data/usage.db` | SQLite path |

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
