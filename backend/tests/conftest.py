from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import create_app
from backend.services.providers.fake import FakeProvider

LOCAL_BASE_URL = "http://127.0.0.1:8000"


@pytest.fixture
def make_app(tmp_path):
    """Factory building an app backed by a temp SQLite DB and a fake provider."""

    def _make(provider=None, **overrides):
        values = dict(
            _env_file=None,
            llm_provider="fake",
            toolloom_data_dir=tmp_path / "data",
            # The suite uses the synthetic demo__add fixture; a normal install
            # ships only the `sandbox` server (see test_seed_ships_only_sandbox).
            toolloom_seed_demo=True,
            toolloom_auto_titles=False,  # title behavior has dedicated scripted tests
        )
        values.update(overrides)
        return create_app(settings=Settings(**values), provider=provider or FakeProvider())

    return _make


@pytest.fixture
def client(make_app):
    with TestClient(make_app(), base_url=LOCAL_BASE_URL) as test_client:
        yield test_client
