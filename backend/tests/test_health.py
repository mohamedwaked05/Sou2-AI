"""Tests for the public health endpoint."""

from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint_returns_service_status() -> None:
    """The health endpoint reports that the local API is available."""
    client = TestClient(app)

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "healthy"
    assert payload["service"] == "Sou2AI API"
    assert payload["version"]


def test_root_endpoint_returns_service_metadata() -> None:
    """The root endpoint identifies the running service and API location."""
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    payload = response.json()
    assert payload["service"] == "Sou2AI API"
    assert payload["status"] == "running"
    assert payload["api"] == "/api/v1"
    assert payload["docs"] == "/docs"
