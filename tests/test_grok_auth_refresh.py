import errno
import json
from datetime import timedelta
from pathlib import Path

from app.models import utcnow
from app.providers.supergrok import (
    _parse_expires_at,
    access_token_needs_refresh,
    save_grok_auth,
    select_grok_auth_entry,
)


def test_parse_expires_at_nanos():
    dt = _parse_expires_at("2026-07-13T12:37:33.710730091Z")
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 7
    assert dt.day == 13


def test_access_token_needs_refresh_expired():
    entry = {
        "key": "tok",
        "expires_at": (utcnow() - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
    }
    assert access_token_needs_refresh(entry) is True


def test_access_token_needs_refresh_fresh():
    entry = {
        "key": "tok",
        "expires_at": (utcnow() + timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
    }
    assert access_token_needs_refresh(entry) is False


def test_select_nested_oidc_entry():
    auth = {
        "https://auth.x.ai::abc": {
            "key": "access",
            "refresh_token": "ref",
            "auth_mode": "oidc",
            "email": "a@b.com",
        }
    }
    scope, entry = select_grok_auth_entry(auth)
    assert scope is not None
    assert entry is not None
    assert entry["email"] == "a@b.com"


def test_save_grok_auth_falls_back_for_bind_mount(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text('{"key":"old"}\n', encoding="utf-8")

    def reject_replace(self: Path, target: Path):
        raise OSError(errno.EBUSY, "bind mount")

    monkeypatch.setattr(Path, "replace", reject_replace)
    save_grok_auth(auth_path, {"key": "new", "refresh_token": "rotated"})

    assert json.loads(auth_path.read_text(encoding="utf-8")) == {
        "key": "new",
        "refresh_token": "rotated",
    }
    assert not auth_path.with_suffix(".json.tmp").exists()
