# Implement: AI Usage Quota Dashboard

## Checklist

1. [x] Create Trellis task artifacts (prd / design / implement)
2. [x] Scaffold `app/` package, config (PORT=50048), requirements, gitignore
3. [x] SQLite repository + models
4. [x] Provider base + DeepSeek + Codex + SuperGrok
5. [x] Poller service + API routes
6. [x] Dashboard HTML/CSS/JS with countdown
7. [x] Docker Compose + .env.example + README
8. [x] Unit tests (Codex windows, DeepSeek balance)
9. [x] Run pytest + smoke uvicorn on 50048 (Codex live OK)

## Validation

```bash
pip install -r requirements.txt
pytest tests/ -q
uvicorn app.main:app --host 0.0.0.0 --port 50048
curl -s localhost:50048/api/health
curl -s localhost:50048/api/usage
```

## Rollback

Remove `app/`, `data/`, Docker artifacts; task remains in Trellis for re-plan.
