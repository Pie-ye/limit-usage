from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_routing_view import make_fake_catalog


def test_lifespan_fails_fast_on_invalid_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    make_fake_catalog(tmp_path)
    tiering = tmp_path / "tiering.yaml"
    tiering.write_text(
        tiering.read_text(encoding="utf-8").replace("T0: {min_bench: 0,", "T0: {min_bench: 1,"),
        encoding="utf-8",
    )
    monkeypatch.setenv("CATALOG_DIR", str(tmp_path))

    with pytest.raises(ValueError, match=r"tiering\.yaml: tiers\.T0\.min_bench"):
        with TestClient(create_app()):
            pass
