# Error Handling

> How errors are handled in this project.

---

## Overview

<!--
Document your project's error handling conventions here.

Questions to answer:
- What error types do you define?
- How are errors propagated?
- How are errors logged?
- How are errors returned to clients?
-->

(To be filled by the team)

---

## Error Types

<!-- Custom error classes/types -->

(To be filled by the team)

---

## Error Handling Patterns

<!-- Try-catch patterns, error propagation -->

(To be filled by the team)

---

## API Error Responses

<!-- Standard error response format -->

(To be filled by the team)

---

## Common Mistakes

<!-- Error handling mistakes your team has made -->

(To be filled by the team)

## Scenario: SuperGrok billing and OIDC refresh

### 1. Scope / Trigger

- Trigger: changes to `app/providers/supergrok.py`, Grok protobuf parsing,
  OIDC refresh, or the Grok Docker secret mount.

### 2. Signatures

- `parse_grok_credits_protobuf(data: bytes) -> list[UsageWindow]`
- `refresh_grok_access_token(client, entry, *, auth_path, auth_root, scope_key)
  -> tuple[str | None, str | None]`
- `save_grok_auth(path: Path, auth: dict[str, Any]) -> None`

### 3. Contracts

- `GROK_AUTH_PATH` points to Grok CLI `auth.json`.
- A credential entry may contain `key`, `refresh_token`, `expires_at`,
  `oidc_issuer`, and `oidc_client_id`.
- `GetGrokCreditsConfig` is a framed gRPC-web protobuf response.
- Proto3 omits zero-valued scalar fields. A config with a valid period start or
  end but no usage-percent field means `used_percent=0.0`, not parse failure.
- The Compose Grok auth-file mount is `:rw`; rotated refresh credentials must
  survive container restarts.

### 4. Validation & Error Matrix

| Condition | Required result |
|---|---|
| Valid billing period, percentage omitted | `ok`, 0% used, 100% remaining |
| Percentage or product usage present | Clamp parsed usage to 0–100% |
| No usage and no recognizable billing period | Return no windows |
| Billing endpoint returns 401/403 | Refresh once, then retry once |
| Refresh succeeds but atomic replace reports `EBUSY`/`EXDEV`/`EPERM` | Flush and fsync the existing bind-mounted file in place |
| Refresh request fails | Return `auth_error`; never log access/refresh tokens |

### 5. Good/Base/Bad Cases

- Good: a normal response has usage plus period timestamps and produces a
  weekly window.
- Base: immediately after reset, timestamps are present and the omitted
  proto3 scalar produces a 0%-used weekly window.
- Bad: an HTTP 200 body without recognized usage or period evidence must not
  invent a window.

### 6. Tests Required

- Captured nonzero fixture: assert usage, remaining percentage, reset, and
  product rows.
- Captured zero-usage fixture: assert one weekly window with 0% used and 100%
  remaining.
- Bind-mount fallback: force `Path.replace` to raise `EBUSY`, then assert the
  target contains rotated credentials and the temporary file is gone.
- Expiry parsing: cover nanosecond timestamps, expired tokens, and fresh tokens.

### 7. Wrong vs Correct

#### Wrong

```python
if used_pct is None and not products:
    return []
```

This treats a proto3 default value omitted from the wire as missing data.

#### Correct

```python
if used_pct is None and (period_start is not None or period_end is not None):
    used_pct = 0.0
```

Only billing-period evidence permits the omitted scalar to be interpreted as
zero; an otherwise unknown payload still fails closed.
