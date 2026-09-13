import pytest

from src.db import models


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A real, isolated local SQLite file - never Turso, regardless of what's in the real
    environment or what some other import happened to load via load_dotenv(). Confirmed
    this matters for real: a test file importing a script module that calls load_dotenv()
    at import time silently routed every test in that run to the live production database,
    since models.connect() prefers Turso whenever these env vars are present at all."""
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    c = models.connect(tmp_path / "test.db")
    yield c
    c.close()
