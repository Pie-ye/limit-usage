# Routing Policy Service

> **Role & Responsibility**: Answers the question *"Which class of model should this agent task use?"*  
> It strictly acts as a declarative routing policy decision layer. It **does NOT execute model inference, does NOT hold any provider credentials, does NOT manage accounts, and is NOT an API gateway**.

---

## 1. Security & Privacy Boundary

### Credential-Free Guarantee
**This service does not hold or store any provider credentials, tokens, cookies, or secrets.** It is an unprivileged decision engine evaluated ahead of model execution.

### Sanitized External Surface
Designed for public Internet exposure through Cloudflare Tunnel, this service enforces a strict data boundary. Under no circumstances will it expose:
- OAuth tokens, API keys, cookies, or session secrets
- Account emails or user identities
- Local filesystem paths (e.g. SQLite paths, credential paths)
- Internal hostnames or network topologies
- Upstream error messages or server exception tracebacks
- Raw provider quota percentages or full upstream telemetry payloads
- Pool IDs or internal provider configurations
- Model benchmark scores (`bench`), blended prices (`blended_price`), or effort (`effort`)
- Cooldown schedules or minute-by-minute rate-limit counters

All unexpected exceptions result in a generic `500 Internal Server Error` with body `{"detail": "internal error"}`. Server logs capture solely the exception class name without tracebacks or upstream request URLs.

---

## 2. API Endpoints

Base path prefix: `/v1`. Only three endpoints exist. Automatic interactive documentation endpoints (`/docs`, `/redoc`, `/openapi.json`) are explicitly disabled to prevent schema enumeration over public tunnel ingress.

### `GET /v1/health`
Public liveness and policy version check. Returns exactly two fields.

```bash
curl -s http://127.0.0.1:50100/v1/health
```

Example response:
```json
{
  "status": "ok",
  "policy_version": "2026-09-26.1"
}
```

### `GET /v1/policy`
Exposes active methodology including tier complexity intervals and review policies. Does not leak model lists, pool names, or benchmark metrics.

```bash
curl -s http://127.0.0.1:50100/v1/policy
```

Example response:
```json
{
  "schema_version": 1,
  "policy_version": "2026-09-26.1",
  "generated_at": "2026-09-26T03:00:00Z",
  "tiers": {
    "T0": {"complexity": [0, 2]},
    "T1": {"complexity": [3, 5]},
    "T2": {"complexity": [6, 8]},
    "T3": {"complexity": [9, 12]}
  },
  "review": {"cross_vendor": true}
}
```

### `POST /v1/recommend`
Evaluates task parameters, caller constraints, and upstream quota signals to determine the optimal model.

```bash
curl -s -X POST http://127.0.0.1:50100/v1/recommend \
  -H "Content-Type: application/json" \
  -d '{
    "tier": "T2",
    "role": "implement",
    "implemented_by_vendor": "codex",
    "client": {"available_vendors": ["openai", "anthropic", "google"]},
    "min_score": 20
  }'
```

Request schema:
- `tier` (required): Task difficulty tier (`T0`, `T1`, `T2`, `T3`).
- `role` (required): Task role (`implement`, `review`, `orchestrate`).
- `implemented_by_vendor` (optional): Vendor of previous implementation to enforce cross-vendor review.
- `client.available_vendors` (optional): Filter to supported vendors. External provider names (`anthropic`, `openai`, `google`, `xai`) and internal CLI pool names (`claude`, `codex`, `agy`, `grok`) are both accepted and mapped automatically. Unknown vendors are ignored.
- `min_score` (optional): Minimum quota score threshold.
- Unknown additional fields are safely ignored.

Example response:
```json
{
  "policy_version": "2026-09-26.1",
  "generated_at": "2026-09-26T03:01:12Z",
  "expires_at": "2026-09-26T03:06:12Z",
  "tier": "T2",
  "role": "implement",
  "recommended": {"vendor": "agy", "model": "gemini-3.8-flash-high", "effort": null},
  "alternatives": [{"vendor": "codex", "model": "gpt-5.6-terra", "effort": "high"}],
  "reason_codes": ["tier_capable", "quota_healthy", "provider_healthy"],
  "wait_seconds": null
}
```

When no candidate meets the criteria, `recommended` returns `null` alongside `reason_codes: ["no_eligible_candidate", "fallback_static"]` with an HTTP 200 status, signaling the client to fall back to its own static ladder.

---

## 3. Configuration Variables

Configured via environment variables using `pydantic-settings`:

| Variable | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Local bind address. **Must strictly default to 127.0.0.1** to prevent unauthenticated network exposure. |
| `PORT` | `50100` | HTTP service port. |
| `POLICY_DIR` | `./policy` | Directory path containing declarative policy YAML definitions (`tiers.yaml`, `roles.yaml`). Shared model catalog is loaded from `CATALOG_DIR` or `/app/catalog`. |
| `LIMIT_USAGE_ROUTING_URL` | `http://127.0.0.1:50048/api/routing` | Upstream limit-usage signal endpoint. |
| `SIGNAL_TTL_SECONDS` | `30` | In-memory cache TTL for capacity signals to prevent upstream thundering herds. |
| `SIGNAL_TIMEOUT_SECONDS` | `3.0` | Upstream HTTP request timeout. |
| `RECOMMEND_TTL_SECONDS` | `300` | Recommendation validity duration used to compute `expires_at`. |

---

## 4. Relationship with `limit-usage`

`routing-policy` operates as a downstream consumer of `limit-usage`'s `GET /api/routing` endpoint.
- **Current State**: Both services coexist. `limit-usage` maintains quota tracking and database polling while `routing-policy` consumes real-time pool health projections to make decoupled routing decisions.
- **Shared Catalog**: Model data and capability derivation are sourced from the repository root `catalog/`. `policy/models.yaml` has been removed. Docker Compose build context is now the repository root (`context: .`) so `catalog/` is included.
- **Signals & Usability**: In addition to pool quota signals, `signals` reads `models.<id>.usable` from `limit-usage`. Models marked unusable by limit-usage (e.g. unlisted in CLI models export, or temporarily marked missing via 404 feedback) will not be recommended.
- **Candidate Ranking in v1**: The `ranking` field in `policy/roles.yaml` (`[availability, quota, capability, cost]`) is descriptive in v1, documenting the intent of candidate evaluation order. The sorting key is evaluated in `engine.py` as `(not eligible, not usable, -score, -bench, blended_price)` to maintain strict behavioral parity with `limit-usage`'s `routing_view` (sorting by quota `score`, then `bench`, then `blended_price`); changing the ranking sequence requires modifying `engine.py`.
- **Future Direction**: The capacity signal layer will be migrated to OmniRoute once deployed, at which point `LIMIT_USAGE_ROUTING_URL` will be updated to point to the new signal provider without breaking the routing policy interface.

---

## 5. Local Verification

Install development dependencies:
```bash
cd routing-policy
/home/pieye/Container/limit-usage/.venv/bin/pip install -r requirements-dev.txt
```

Start the development server from the service directory:
```bash
cd routing-policy
/home/pieye/Container/limit-usage/.venv/bin/python -m app.main
```

The module entrypoint reads `HOST` and `PORT` from the environment and defaults to
`127.0.0.1:50100` when they are not set.

Run the test suite:
```bash
cd routing-policy
/home/pieye/Container/limit-usage/.venv/bin/python -m pytest tests/ -v
```

Verify Docker Compose configuration from repository root:
```bash
cd ..
docker compose config -q && echo "compose ok"
```
