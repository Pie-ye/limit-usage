from app.services import ups


SAMPLE = """
  native-path:          /sys/devices/.../hiddev1
  vendor:               CPS
  model:                CP1000AVRLCDa
  serial:               CY8RP2000612
  power supply:         yes
  ups
    present:             yes
    state:               fully-charged
    warning-level:       none
    time to empty:       4.9 hours
    percentage:          100%
"""


def test_parse_upower_sample(monkeypatch):
    class R:
        returncode = 0
        stdout = SAMPLE
        stderr = ""

    monkeypatch.setattr(ups.shutil, "which", lambda _: "/usr/bin/upower")
    monkeypatch.setattr(ups.subprocess, "run", lambda *a, **k: R())
    out = ups.read_ups()
    assert out["status"] == "ok"
    assert out["percent"] == 100.0
    assert out["state"] == "fully-charged"
    assert out["state_display"] == "已充滿"
    assert out["on_battery"] is False
    assert out["model"] == "CP1000AVRLCDa"
    assert abs(out["time_to_empty_hours"] - 4.9) < 0.01


def test_ups_missing_binary(monkeypatch):
    monkeypatch.setattr(ups.shutil, "which", lambda _: None)
    out = ups.read_ups()
    assert out["status"] == "error"
    assert out["percent"] is None
