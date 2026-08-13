"""API behavior for safe database health reporting."""

from collections.abc import Generator
from typing import Any

from app.database.session import get_db_session
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError


def test_database_health_is_healthy_when_postgresql_is_reachable(
    database_engine: object,
) -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/health/database")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


class UnavailableSession:
    def execute(self, _statement: object) -> None:
        raise OperationalError("SELECT 1", {}, ConnectionError("secret-db-host:5432"))


def unavailable_session() -> Generator[Any]:
    yield UnavailableSession()


def test_database_health_failure_is_safe() -> None:
    app.dependency_overrides[get_db_session] = unavailable_session
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/health/database")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
    assert "secret-db-host" not in response.text
    assert "5432" not in response.text
