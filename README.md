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

## Permanent run + start on boot (systemd user)

```bash
# once: venv + .env ready
./deploy/install-user-service.sh
```

This installs `~/.config/systemd/user/limit-usage.service`, enables it, and starts it now.
Your account already uses `loginctl linger` so the service comes up after reboot without logging in.

```bash
systemctl --user status limit-usage
systemctl --user restart limit-usage
journalctl --user -u limit-usage -f
```

### Docker (alternative)

```bash
docker compose up --build -d
```

`restart: unless-stopped` keeps the container up; enable Docker itself on boot (`systemctl enable docker`).
Mounts `~/.codex/auth.json` and `~/.grok/auth.json` read-only. Set `DEEPSEEK_API_KEY` in compose or env.

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
