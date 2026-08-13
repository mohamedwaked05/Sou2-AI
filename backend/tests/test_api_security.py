"""HTTP-boundary, logging, proxy, and injection regression tests."""

import json
import logging
import uuid

import pytest
from app.core.config import Settings
from app.core.logging import (
    PrivacySafeConsoleFormatter,
    ProductionJSONFormatter,
    redact_log_value,
)
from app.core.network import resolve_client_ip
from app.database.models import Business, User
from app.main import create_app
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.test_business_api import create_draft, create_user, headers
from tests.test_owner_chat import active_business, submit


def production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "environment": "production",
        "debug": False,
        "trusted_hosts": ["api.example.com"],
        "allowed_cors_origins": ["https://app.example.com"],
        "access_token_secret": SecretStr("a-production-secret-that-is-long-enough"),
        "refresh_cookie_secure": True,
        "resend_api_key": SecretStr("production-placeholder-key"),
    }
    values.update(overrides)
    return Settings(**values)


def test_server_request_id_ignores_client_value_and_all_errors_include_it(
    api_client: TestClient,
) -> None:
    response = api_client.get(
        "/api/v1/businesses/not-a-uuid",
        headers={"X-Request-ID": "attacker-controlled"},
    )
    request_id = response.headers["x-request-id"]
    assert response.status_code == 401
    assert request_id != "attacker-controlled"
    assert uuid.UUID(request_id)
    assert response.json()["error"]["request_id"] == request_id


def test_trusted_hosts_reject_invalid_host_before_routes() -> None:
    application = create_app(
        Settings(
            _env_file=None,
            environment="testing",
            trusted_hosts=["testserver", "localhost", "127.0.0.1"],
        )
    )
    client = TestClient(application)
    rejected = client.get("/api/v1/health", headers={"Host": "evil.example"})
    allowed = client.get("/api/v1/health", headers={"Host": "localhost:8000"})
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "invalid_host"
    assert allowed.status_code == 200


def test_body_limit_rejects_declared_and_streamed_oversize_payloads() -> None:
    application = create_app(
        Settings(
            _env_file=None,
            environment="testing",
            trusted_hosts=["testserver"],
            max_request_body_bytes=16,
        )
    )
    client = TestClient(application)
    declared = client.post(
        "/api/v1/auth/login",
        content=b"x" * 17,
        headers={"Content-Type": "application/json"},
    )
    streamed = client.post(
        "/api/v1/auth/login",
        content=(chunk for chunk in (b"12345678", b"123456789")),
        headers={"Content-Type": "application/json", "Content-Length": "1"},
    )
    for response in (declared, streamed):
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "request_body_too_large"
        assert (
            response.json()["error"]["request_id"] == response.headers["x-request-id"]
        )


def _request(peer: str, forwarded: str | None = None) -> Request:
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (peer, 1234),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


def test_proxy_cidr_chain_handling_blocks_spoofing_and_malformed_values() -> None:
    untrusted = Settings(_env_file=None, trusted_proxy_cidrs=[])
    trusted = Settings(
        _env_file=None,
        trusted_proxy_cidrs=["10.0.0.0/8", "2001:db8::/32"],
    )
    assert (
        resolve_client_ip(_request("203.0.113.9", "198.51.100.7"), untrusted)
        == "203.0.113.9"
    )
    assert (
        resolve_client_ip(_request("10.0.0.2", "198.51.100.7, 10.0.0.3"), trusted)
        == "198.51.100.7"
    )
    assert (
        resolve_client_ip(_request("2001:db8::2", "2001:db8:1::5"), trusted)
        == "2001:db8:1::5"
    )
    assert (
        resolve_client_ip(_request("10.0.0.2", "not-an-ip, 10.0.0.3"), trusted)
        == "10.0.0.2"
    )


def test_cors_security_headers_and_production_docs_hsts_configuration() -> None:
    testing = create_app(
        Settings(
            _env_file=None,
            environment="testing",
            trusted_hosts=["testserver"],
            allowed_cors_origins=["https://ui.example"],
        )
    )
    client = TestClient(testing)
    preflight = client.options(
        "/api/v1/health",
        headers={
            "Origin": "https://ui.example",
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "Authorization,Content-Type,Accept",
        },
    )
    assert preflight.status_code == 200
    assert set(preflight.headers["access-control-allow-methods"].split(", ")) == {
        "GET",
        "POST",
        "PATCH",
        "DELETE",
        "OPTIONS",
    }
    response = client.get("/api/v1/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in response.headers

    production = production_settings(
        hsts_enabled=True,
        trusted_https_termination=True,
    )
    production_client = TestClient(
        create_app(production), base_url="https://api.example.com"
    )
    assert production_client.get("/docs").status_code == 404
    assert production_client.get("/openapi.json").status_code == 404
    assert "docs" not in production_client.get("/").json()
    assert (
        "max-age="
        in production_client.get("/api/v1/health").headers["strict-transport-security"]
    )
    try:
        Settings(
            _env_file=None,
            environment="production",
            trusted_hosts=["*"],
        )
    except ValidationError:
        pass
    else:  # pragma: no cover - protects a production startup invariant
        raise AssertionError("Production wildcard host was accepted.")


@pytest.mark.parametrize(
    "host",
    ["LOCALHOST", "localhost.", "127.0.0.1", "::1", "[::1]"],
)
def test_production_rejects_normalized_loopback_only_hosts(host: str) -> None:
    with pytest.raises(ValidationError):
        production_settings(trusted_hosts=[host])


def test_trusted_hosts_normalize_dns_case_and_trailing_dot() -> None:
    settings = production_settings(trusted_hosts=[" API.Example.COM. "])
    assert settings.trusted_hosts == ["api.example.com"]
    client = TestClient(create_app(settings), base_url="https://api.example.com")
    assert (
        client.get(
            "/api/v1/health", headers={"Host": "API.EXAMPLE.COM.:443"}
        ).status_code
        == 200
    )
    with pytest.raises(ValidationError):
        production_settings(trusted_hosts=["api.example.com:443"])
    assert (
        client.get(
            "/api/v1/health", headers={"Host": "api.example.com:65536"}
        ).status_code
        == 400
    )


@pytest.mark.parametrize(
    "origin",
    [
        "HTTP://LOCALHOST:5173",
        "http://localhost.:5173",
        "http://127.0.0.1:5173",
        "http://[::1]:5173",
        "http://[0:0:0:0:0:0:0:1]:5173",
        "https://user:pass@app.example.com",
        "https://app.example.com/path",
        "https://app.example.com/?query=value",
        "https://app.example.com/#fragment",
        "ftp://app.example.com",
        "https://exa mple.com",
        "*",
    ],
)
def test_production_rejects_local_or_malformed_cors_origins(origin: str) -> None:
    with pytest.raises(ValidationError):
        production_settings(allowed_cors_origins=[origin])


@pytest.mark.parametrize(
    "origin",
    [
        "https://app.example.com:",
        "http://app.example.com:",
        "http://127.0.0.1:",
        "http://[::1]:",
        "https://[2001:db8::1]:",
        "https://app.example.com:0",
        "https://app.example.com:65536",
        "https://app.example.com:not-a-port",
        "https://[2001:db8::1",
        "https://[not-ipv6]:443",
    ],
)
def test_cors_origins_reject_empty_or_invalid_ports(origin: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, allowed_cors_origins=[origin])


@pytest.mark.parametrize(
    ("origin", "normalized"),
    [
        ("https://app.example.com", "https://app.example.com"),
        ("https://app.example.com/", "https://app.example.com"),
        ("https://app.example.com:443", "https://app.example.com"),
        ("http://app.example.com:80", "http://app.example.com"),
        ("https://app.example.com:8443", "https://app.example.com:8443"),
        ("https://[2001:db8::1]:8443", "https://[2001:db8::1]:8443"),
    ],
)
def test_production_cors_origins_accept_complete_valid_ports(
    origin: str, normalized: str
) -> None:
    settings = production_settings(allowed_cors_origins=[origin])
    assert settings.allowed_cors_origins == [normalized]


def test_cors_origins_normalize_valid_domains_and_allow_development_loopback() -> None:
    production = production_settings(
        allowed_cors_origins=["HTTPS://App.Example.COM.:443/"]
    )
    assert production.allowed_cors_origins == ["https://app.example.com"]
    development = Settings(
        _env_file=None,
        trusted_hosts=["LOCALHOST."],
        allowed_cors_origins=["HTTP://LOCALHOST:5173/", "http://[::1]:5173"],
    )
    assert development.trusted_hosts == ["localhost"]
    assert development.allowed_cors_origins == [
        "http://localhost:5173",
        "http://[::1]:5173",
    ]


def test_unexpected_errors_are_generic_even_with_debug_enabled() -> None:
    application = create_app(
        Settings(
            _env_file=None,
            environment="testing",
            debug=True,
            trusted_hosts=["testserver"],
        )
    )

    @application.get("/test-unexpected")
    def unexpected() -> None:
        raise RuntimeError("secret filesystem C:\\private\\token.txt")

    client = TestClient(application, raise_server_exceptions=False)
    response = client.get("/test-unexpected")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_server_error"
    assert "secret" not in response.text
    assert "private" not in response.text


def test_production_json_logging_and_redaction() -> None:
    formatter = ProductionJSONFormatter()
    record = logging.LogRecord(
        name="sou2ai.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request_completed",
        args=(),
        exc_info=None,
    )
    record.request_id = str(uuid.uuid4())
    record.http_method = "GET"
    record.route_template = "/api/v1/businesses/{business_id}"
    record.status_code = 200
    payload = json.loads(formatter.format(record))
    assert payload["event"] == "request_completed"
    assert payload["route_template"] == "/api/v1/businesses/{business_id}"
    assert redact_log_value("Authorization: Bearer abc.def.ghi") == (
        "Authorization: [REDACTED] [REDACTED]"
    )
    redacted = redact_log_value(
        "password=hunter2 database_url=postgresql://user:pass@host/db"
    )
    assert "hunter2" not in redacted
    assert "user:pass" not in redacted


def test_development_exception_logging_redacts_raw_details() -> None:
    secret = RuntimeError("SQL failed at C:\\private\\secret.sql")
    record = logging.LogRecord(
        name="sou2ai.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="unexpected failure",
        args=(),
        exc_info=(RuntimeError, secret, None),
    )
    rendered = PrivacySafeConsoleFormatter("%(message)s").format(record)
    assert "RuntimeError: [REDACTED_INTERNAL_EXCEPTION]" in rendered
    assert "private" not in rendered
    assert "secret.sql" not in rendered


def test_sql_injection_payloads_remain_data_and_do_not_bypass_tenant_scope(
    api_client: TestClient, db_session: Session
) -> None:
    owner = create_user(db_session, "sql-owner@example.com")
    foreign = create_user(db_session, "sql-foreign@example.com")
    payload = "'; DROP TABLE businesses; --"
    business = create_draft(api_client, owner, payload)
    assert business["name"] == payload
    hidden = api_client.get(
        f"/api/v1/businesses/{business['id']}", headers=headers(foreign)
    )
    assert hidden.status_code == 404
    assert db_session.scalar(select(func.count()).select_from(Business)) == 1
    assert db_session.scalar(select(func.count()).select_from(User)) == 2


def test_sql_injection_payloads_are_inert_in_auth_chat_cursor_and_usage(
    api_client: TestClient, db_session: Session
) -> None:
    payload = "' OR 1=1 --"
    registration = api_client.post(
        "/api/v1/auth/register",
        json={
            "first_name": "SQL",
            "last_name": "Probe",
            "email": payload,
            "password": "Strong1!Pass",
            "password_confirmation": "Strong1!Pass",
        },
    )
    assert registration.status_code == 422

    owner, business = active_business(
        api_client,
        db_session,
        email="sql-chat@example.com",
        name="SQL Chat Market",
    )
    turn = submit(api_client, owner, business["id"], payload, key=payload)
    assert turn.status_code == 200
    assert turn.json()["owner_message"]["content"] == payload
    cursor = api_client.get(
        f"/api/v1/businesses/{business['id']}/owner-chat/messages",
        headers=headers(owner),
        params={"cursor": "'; DROP TABLE businesses; --"},
    )
    assert cursor.status_code == 422
    invalid_usage_id = api_client.get(
        f"/api/v1/businesses/{payload}/ai-usage/current", headers=headers(owner)
    )
    assert invalid_usage_id.status_code == 422
    assert db_session.scalar(select(func.count()).select_from(Business)) == 1
    openapi = api_client.get("/openapi.json").json()
    assert not any("sql" in path.casefold() for path in openapi["paths"])
