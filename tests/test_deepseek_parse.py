from app.providers.deepseek import parse_balance_payload


def test_parse_balance_prefers_usd():
    data = {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "CNY",
                "total_balance": "110.00",
                "granted_balance": "10.00",
                "topped_up_balance": "100.00",
            },
            {
                "currency": "USD",
                "total_balance": "12.50",
                "granted_balance": "2.50",
                "topped_up_balance": "10.00",
            },
        ],
    }
    windows = parse_balance_payload(data)
    assert windows[0].currency == "USD"
    assert windows[0].amount == "12.50"
    assert windows[0].raw_extra["granted_balance"] == "2.50"
    assert windows[0].raw_extra["is_available"] is True
    assert windows[0].resets_at is None


def test_parse_empty_balance():
    windows = parse_balance_payload({"is_available": False, "balance_infos": []})
    assert len(windows) == 1
    assert windows[0].amount == "0"
