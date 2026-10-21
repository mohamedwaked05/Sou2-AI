"""Ordinary offline flows explicitly opt in; activation tests verify closed mode."""

import pytest
from app.core.config import get_settings


@pytest.fixture(autouse=True)
def explicit_offline_admission(monkeypatch):
    monkeypatch.setenv("ADMISSION_MODE", "open")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
