from datetime import datetime, timezone

from app.providers.supergrok import parse_grok_credits_protobuf, parse_supergrok_payload


def test_parse_billing_rpc_shape():
    data = {
        "billingCycle": {
            "billingPeriodStart": "2026-07-01T00:00:00Z",
            "billingPeriodEnd": "2026-07-08T00:00:00Z",
        },
        "monthlyLimit": {"val": 10000},
        "usage": {
            "totalUsed": {"val": 2500},
        },
    }
    windows = parse_supergrok_payload(data)
    assert len(windows) == 1
    assert windows[0].used_percent == 25.0
    assert windows[0].remaining_percent == 75.0
    assert windows[0].resets_at is not None


def test_parse_flat_percent():
    data = {"used_percent": 80, "resets_at": "2026-07-15T00:00:00Z"}
    windows = parse_supergrok_payload(data)
    assert windows[0].remaining_percent == 20.0


def test_parse_grok_credits_protobuf_fixture():
    # Live capture: grpc-web frame + GetGrokCreditsConfig body
    # used 46%, period 2026-07-11 → 2026-07-18, products ~44% + 2%
    raw = bytes.fromhex(
        "000000005f"
        "0a5d0d0000384212001a00220c08c9a9c8d20610b8ecfdb602"
        "2a0c08c99eedd20610b8ecfdb602"
        "3a0708011500003042"
        "3a0708021500000040"
        "421e0802120c08c9a9c8d20610b8ecfdb6021a0c08c99eedd20610b8ecfdb602"
        "580162006801"
    )
    windows = parse_grok_credits_protobuf(raw)
    assert windows, "expected at least main weekly window"
    main = windows[0]
    assert main.key == "weekly"
    assert abs(main.used_percent - 46.0) < 0.01
    assert abs(main.remaining_percent - 54.0) < 0.01
    assert main.resets_at is not None
    assert main.resets_at.astimezone(timezone.utc).day == 18
    assert any(w.key.startswith("product-") for w in windows)
