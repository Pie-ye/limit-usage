from app.providers.deepseek import parse_balance_payload


def test_parse_balance_cny_only_drops_usd():
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
    assert len(windows) == 1
    assert windows[0].currency == "CNY"
    assert windows[0].amount == "110.00"
    assert windows[0].raw_extra["granted_balance"] == "10.00"
    assert windows[0].raw_extra["is_available"] is True
    assert windows[0].resets_at is None


def test_parse_usd_only_returns_zero_cny_placeholder():
    data = {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "USD",
                "total_balance": "12.50",
                "granted_balance": "0",
                "topped_up_balance": "12.50",
            },
        ],
    }
    windows = parse_balance_payload(data)
    assert len(windows) == 1
    assert windows[0].currency == "CNY"
    assert windows[0].amount == "0"


def test_parse_empty_balance():
    windows = parse_balance_payload({"is_available": False, "balance_infos": []})
    assert len(windows) == 1
    assert windows[0].amount == "0"
    assert windows[0].currency == "CNY"
