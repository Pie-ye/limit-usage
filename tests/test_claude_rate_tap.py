from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TAP = ROOT / "deploy" / "claude_rate_tap.py"
WRAPPER = ROOT / "deploy" / "claude-rate-tap"

sys.path.insert(0, str(ROOT / "deploy"))
from claude_rate_tap import merge_capture  # noqa: E402


def _event(unified: dict) -> bytes:
    return json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "allowed", "unifiedWindows": unified},
            "uuid": "u",
            "session_id": "s",
        },
        separators=(",", ":"),
    ).encode()


def _run_tap(stdin: bytes, capture: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "CLAUDE_RATE_TAP_CAPTURE": str(capture)}
    return subprocess.run(
        [sys.executable, str(TAP)], input=stdin, capture_output=True, env=env, timeout=10
    )


def test_stream_is_forwarded_byte_for_byte_and_windows_recorded(tmp_path):
    future = int(time.time()) + 3600
    stream = b"".join(
        [
            b'{"type":"system","subtype":"init"}\n',
            b"not json at all\n",
            b'{"type":"rate_limit_event",broken\n',
            _event(
                {
                    "five_hour": {"utilization": 0.23, "resetsAt": future},
                    "seven_day": {"utilization": 0.5, "resetsAt": future + 1},
                    "seven_day_overage_included": {"utilization": 0.071, "resetsAt": future + 2},
                }
            )
            + b"\n",
            "中文   text\n".encode(),
            b'{"type":"result"}',  # no trailing newline
        ]
    )
    capture = tmp_path / "stream" / "latest.json"
    result = _run_tap(stream, capture)
    assert result.returncode == 0
    assert result.stdout == stream

    payload = json.loads(capture.read_text())
    assert abs(payload["captured_at_epoch"] - time.time()) < 30
    limits = payload["rate_limits"]
    assert limits["five_hour"] == {"used_percentage": 23.0, "resets_at": future}
    assert limits["seven_day"] == {"used_percentage": 50.0, "resets_at": future + 1}
    assert limits["model_scoped"] == [
        {"displayName": "Fable", "limit": {"utilization": 7.1, "resets_at": future + 2}}
    ]


def test_capture_parses_with_the_provider(tmp_path):
    from app.providers.claude import parse_statusline_capture

    future = int(time.time()) + 3600
    capture = tmp_path / "latest.json"
    _run_tap(
        _event(
            {
                "five_hour": {"utilization": 0.1, "resetsAt": future},
                "seven_day_overage_included": {"utilization": 0.2, "resetsAt": future},
            }
        )
        + b"\n",
        capture,
    )
    _observed, windows, reason = parse_statusline_capture(json.loads(capture.read_text()))
    assert reason is None
    assert {w.key: w.used_percent for w in windows} == {"5h": 10.0, "1w-fable": 20.0}


def test_windows_missing_from_an_event_keep_their_last_value():
    now = time.time()
    previous = {
        "captured_at_epoch": int(now) - 60,
        "rate_limits": {
            "five_hour": {"used_percentage": 10, "resets_at": int(now) + 100},
            "seven_day": {"used_percentage": 40, "resets_at": int(now) - 1},  # already reset
            "model_scoped": [
                {"displayName": "Fable", "limit": {"utilization": 5, "resets_at": int(now) + 100}}
            ],
        },
    }
    merged = merge_capture(
        previous, {"five_hour": {"utilization": 0.12, "resetsAt": int(now) + 100}}, now
    )
    limits = merged["rate_limits"]
    assert limits["five_hour"]["used_percentage"] == 12.0
    assert "seven_day" not in limits
    assert limits["model_scoped"][0]["limit"]["utilization"] == 5


def test_event_without_usable_windows_does_not_touch_the_capture():
    assert merge_capture(None, {"five_hour": {"utilization": "x"}}, time.time()) is None


def test_unwritable_capture_still_forwards_everything(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    stream = _event({"five_hour": {"utilization": 0.1, "resetsAt": 2**31}}) + b"\n" + b"tail\n"
    result = _run_tap(stream, blocker / "sub" / "latest.json")
    assert result.returncode == 0
    assert result.stdout == stream


def test_large_stream_is_forwarded_intact(tmp_path):
    stream = b"".join(b'{"type":"assistant","n":%d,"pad":"%s"}\n' % (i, b"x" * 500) for i in range(5000))
    stream += b"y" * (3 << 20) + b"\n" + _event({"five_hour": {"utilization": 0.3, "resetsAt": 2**31}}) + b"\n"
    capture = tmp_path / "latest.json"
    result = _run_tap(stream, capture)
    assert result.stdout == stream
    assert json.loads(capture.read_text())["rate_limits"]["five_hour"]["used_percentage"] == 30.0


def test_wrapper_passes_stdin_args_and_exit_code(tmp_path):
    fake = tmp_path / "fake-claude"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'echo "args:$*"\n'
        "read -r line\n"
        'echo "stdin:$line"\n'
        "printf '%s\\n' "
        + "'"
        + _event({"seven_day": {"utilization": 0.42, "resetsAt": 2**31}}).decode()
        + "'\n"
        "exit 3\n"
    )
    fake.chmod(0o755)
    capture = tmp_path / "latest.json"
    env = {
        **os.environ,
        "CLAUDE_RATE_TAP_REAL": str(fake),
        "CLAUDE_RATE_TAP_CAPTURE": str(capture),
    }
    result = subprocess.run(
        [str(WRAPPER), "--output-format", "stream-json"],
        input=b"hello\n",
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 3
    lines = result.stdout.decode().splitlines()
    assert lines[0] == "args:--output-format stream-json"
    assert lines[1] == "stdin:hello"
    # The tap may still be finishing its write when the wrapper's exit is seen.
    for _ in range(50):
        if capture.exists():
            break
        time.sleep(0.05)
    assert json.loads(capture.read_text())["rate_limits"]["seven_day"]["used_percentage"] == 42.0
