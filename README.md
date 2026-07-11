# limit-usage

Self-hosted dashboard for AI provider quotas:

| Provider | What you see |
|----------|----------------|
| **Codex** | 5-hour + weekly remaining % and reset countdown |
| **SuperGrok** | Weekly / billing pool % and reset (best-effort) |
| **DeepSeek** | API balance (total / granted / topped-up) |

Default URL: **http://localhost:50048**

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env: DEEPSEEK_API_KEY, auth paths if needed
python -m uvicorn app.main:app --host 0.0.0.0 --port 50048
```

Open http://localhost:50048

### Docker

```bash
docker compose up --build
```

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
- `POST /api/refresh` — force poll (rate-limited)
- `GET /api/history?provider=&limit=`

## Notes

- **Codex** uses `GET https://chatgpt.com/backend-api/wham/usage`. Windows are classified by `limit_window_seconds` (~18000 = 5h, ~604800 = 1w).
- **DeepSeek** uses official `GET /user/balance`. No weekly reset.
- **SuperGrok** has no stable public usage API. The app tries auth.json + best-effort HTTP endpoints and degrades cleanly if they fail. Other providers keep working.
- Snapshots are stored in SQLite so restarts still show the last successful data.
- Do not expose this service to the public internet without your own auth layer; tokens live on the host.

## Tests

```bash
pytest tests/ -q
```

## License

Private / personal use.
