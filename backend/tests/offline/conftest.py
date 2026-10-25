"""Ordinary offline flows explicitly opt in; activation tests verify closed mode."""

import pytest
from app.core.config import get_settings


@pytest.fixture(autouse=True)
def explicit_offline_admission(monkeypatch):
    monkeypatch.setenv("ADMISSION_MODE", "open")
    monkeypatch.setenv("GEMINI_API_KEY", "offline-test-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
