import pytest

from src.db import models


@pytest.fixture
def conn(tmp_path):
    c = models.connect(tmp_path / "test.db")
    yield c
    c.close()
