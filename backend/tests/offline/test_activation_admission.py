"""Actual ASGI denial and independent producers, with external I/O forbidden."""

import asyncio
import socket
import sys
import uuid
from unittest.mock import Mock

import pytest
from app.core.config import Settings, get_settings
from app.core.exceptions import ApplicationError
from app.database.session import get_db_session
from app.main import create_app
from app.services import (
    customer_channels,
    customer_messaging,
    knowledge_documents,
    owner_chat,
    source_mapping,
)
from app.services.email import ResendEmailService
from app.worker import conversation_summary, customer_messages, knowledge
from fastapi.testclient import TestClient
from sqlalchemy import Engine


def configured(**updates):
    return Settings(
        _env_file=None,
        environment="testing",
        owner_chat_provider="mock",
        source_mapping_provider="mock",
        conversation_summary_provider="mock",
        customer_chat_provider="mock",
        trusted_hosts=["testserver"],
        **updates,
    )


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    connect = socket.socket.connect

    def deny_connect(sock, address):
        # Permit only asyncio's stdlib self-pipe, not general loopback traffic.
        if (
            sys._getframe(1).f_code
            is getattr(socket, "_fallback_socketpair", lambda: None).__code__
        ):
            return connect(sock, address)
        pytest.fail("Activation admission test attempted external I/O")

    def deny(*args, **kwargs):
        pytest.fail("Activation admission test attempted external I/O")

    monkeypatch.setattr(socket.socket, "connect", deny_connect)
    monkeypatch.setattr(Engine, "connect", deny)
    monkeypatch.setenv("ADMISSION_MODE", "closed")
    get_settings.cache_clear()


def test_unset_admission_defaults_closed(monkeypatch):
    monkeypatch.delenv("ADMISSION_MODE")
    assert configured().admission_mode == "closed"


def test_invalid_admission_configuration_fails_closed():
    with pytest.raises(ValueError):
        configured(admission_mode="typo")


def test_actual_metadata_json_has_integer_contract():
    with TestClient(create_app(configured())) as client:
        response = client.get("/")
    assert response.status_code == 200
    value = response.json()
    assert type(value["worker_contract_version"]) is int
    assert value["worker_contract_version"] == 3
    assert value["admission_mode"] == "closed"


def test_every_registered_route_is_closed_except_reviewed_get_probes():
    app = create_app(configured())
    allowed = {"/", "/api/v1/health", "/api/v1/health/database"}
    reached = []

    class ReadOnlyProbe:
        def execute(self, statement):
            assert str(statement) == "SELECT 1"
            reached.append("database_probe")

    app.dependency_overrides[get_db_session] = lambda: ReadOnlyProbe()
    cases = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if method in {"get", "post", "put", "patch", "delete", "options", "head"}
    }
    cases.update(
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", ())
    )
    assert len(cases) > 20
    with TestClient(app) as client:
        for method, path in sorted(cases):
            response = client.request(method, path, content=b"private-body")
            expected = 200 if method == "GET" and path in allowed else 503
            assert response.status_code == expected, (method, path)
            if expected == 503:
                if method != "HEAD":
                    assert response.json()["error"]["code"] == "admission_closed"
                assert response.headers["x-request-id"]
                assert "private-body" not in response.text
    assert reached == ["database_probe"]


@pytest.mark.parametrize(
    "method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
)
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/owner-chat",
        "/api/v1/webhooks/whatsapp",
        "/api/v1/unknown",
        "/api/v1/health/",
        "/docs",
    ],
)
def test_direct_ingress_has_no_submission_or_unknown_route_bypass(method, path):
    with TestClient(create_app(configured())) as client:
        response = client.request(method, path)
    assert response.status_code == 503


def test_closed_barrier_rejects_before_reading_request_body():
    app = create_app(configured())
    sent = []

    async def receive():
        pytest.fail("Closed admission read the request payload")

    async def send(message):
        sent.append(message)

    asyncio.run(
        app(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/owner-chat",
                "headers": [(b"host", b"testserver")],
                "query_string": b"",
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 1),
            },
            receive,
            send,
        )
    )
    assert sent[0]["status"] == 503


def test_closed_websocket_never_reaches_application():
    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(create_app(configured())({"type": "websocket"}, Mock(), send))
    assert sent == [{"type": "websocket.close", "code": 1013}]


@pytest.mark.parametrize(
    "producer",
    [
        lambda s: owner_chat.submit_owner_message(
            None, None, uuid.uuid4(), None, None, s
        ),
        lambda s: source_mapping.propose_mapping(
            None, None, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), None, None
        ),
        lambda s: source_mapping.discover_source(
            None, None, uuid.uuid4(), uuid.uuid4(), None
        ),
        lambda s: knowledge_documents.upload(None, None, uuid.uuid4(), None, s),
        lambda s: knowledge_documents.retry(None, None, uuid.uuid4(), uuid.uuid4(), s),
        lambda s: customer_messaging.ingest_inbound_event(None, None, s),
        lambda s: customer_messaging.ingest_delivery_event(None, None),
        lambda s: customer_channels.create_manual_reply(
            None, None, uuid.uuid4(), uuid.uuid4(), "private"
        ),
        lambda s: conversation_summary.enqueue_conversation_summary(uuid.uuid4(), s),
        lambda s: knowledge.enqueue_document(uuid.uuid4(), s),
        lambda s: customer_messages.enqueue_inbound_message(uuid.uuid4(), s),
        lambda s: customer_messages.enqueue_outbound_message(uuid.uuid4(), s),
        lambda s: customer_messages.recover_expired_customer_message_claims(s),
        lambda s: conversation_summary.process_conversation_summary(str(uuid.uuid4())),
        lambda s: knowledge.process_document(str(uuid.uuid4())),
        lambda s: customer_messages.process_inbound_message(
            str(uuid.uuid4()), settings_override=s
        ),
        lambda s: customer_messages.process_outbound_message(
            str(uuid.uuid4()), settings_override=s
        ),
        lambda s: ResendEmailService(s).send_verification_email(
            "private@example.invalid", "private-token"
        ),
    ],
)
def test_independently_called_producers_reject_before_state_or_dispatch(
    producer, monkeypatch
):
    def deny(*args, **kwargs):
        pytest.fail("Closed admission reached Redis")

    for module in (knowledge, conversation_summary, customer_messages):
        monkeypatch.setattr(module.Redis, "from_url", deny)
        monkeypatch.setattr(module, "get_session_factory", deny)
    with pytest.raises(ApplicationError) as error:
        producer(configured())
    assert error.value.error_code == "admission_closed"
    assert error.value.status_code == 503


def test_open_mode_preserves_normal_route_authorization():
    with TestClient(create_app(configured(admission_mode="open"))) as client:
        assert client.get("/api/v1/unknown").status_code == 404
