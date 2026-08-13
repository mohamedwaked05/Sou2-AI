"""Milestone 5 owner chat, knowledge, idempotency, and isolation tests."""

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Lock

import httpx
import pytest
from app.agent.owner_chat_provider import (
    OllamaOwnerChatProvider,
    OwnerChatProviderTimeout,
    OwnerChatRequest,
    OwnerChatResult,
    ProposedKnowledge,
    get_owner_chat_provider,
)
from app.core.config import get_settings
from app.database.models import (
    BusinessKnowledge,
    ChatMessageRole,
    OwnerChatMessage,
    OwnerConversation,
    User,
)
from app.main import app
from app.schemas.owner_chat import OwnerMessageRequest
from app.services.owner_chat import submit_owner_message
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tests.test_business_api import (
    change_business_status,
    complete_profile,
    create_draft,
    create_user,
    headers,
    valid_hours,
)


class CapturingProvider:
    def __init__(self, result: OwnerChatResult | None = None) -> None:
        self.requests: list[OwnerChatRequest] = []
        self.result = result or OwnerChatResult(reply="A deterministic test reply.")

    def generate(self, request: OwnerChatRequest) -> OwnerChatResult:
        self.requests.append(request)
        return self.result


class TimeoutProvider:
    def generate(self, request: OwnerChatRequest) -> OwnerChatResult:
        raise OwnerChatProviderTimeout


def active_business(
    client: TestClient,
    session: Session,
    *,
    email: str = "chat-owner@example.com",
    name: str = "Owner Chat Market",
) -> tuple[object, dict[str, object]]:
    user = create_user(session, email)
    business = create_draft(client, user, name)
    completed = complete_profile(client, user, str(business["id"]))
    assert completed.status_code == 200
    confirmed = client.post(
        f"/api/v1/businesses/{business['id']}/onboarding/confirm",
        headers=headers(user),
    )
    assert confirmed.status_code == 200
    change_business_status(session, business["id"], "ACTIVE")
    return user, business


def submit(
    client: TestClient,
    user: object,
    business_id: object,
    content: str,
    key: str = "turn-1",
) -> object:
    return client.post(
        f"/api/v1/businesses/{business_id}/owner-chat/messages",
        headers=headers(user),
        json={"idempotency_key": key, "content": content},
    )


def test_chat_authorization_activation_and_profile_requirements(
    api_client: TestClient, db_session: Session
) -> None:
    owner = create_user(db_session, "eligibility@example.com")
    foreign = create_user(db_session, "foreign-chat@example.com")
    draft = create_draft(api_client, owner, "Eligibility Market")
    path = f"/api/v1/businesses/{draft['id']}/owner-chat/messages"
    body = {"idempotency_key": "eligibility", "content": "Hello"}

    assert api_client.post(path, json=body).status_code == 401
    hidden = api_client.post(path, json=body, headers=headers(foreign))
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "business_not_found"
    inactive = api_client.post(path, json=body, headers=headers(owner))
    assert inactive.status_code == 403
    assert inactive.json()["error"]["code"] == "business_not_active"
    assert inactive.json()["error"]["message"] == "This business is not active."
    assert inactive.json()["error"]["request_id"] == inactive.headers["x-request-id"]

    complete_profile(api_client, owner, str(draft["id"]))
    still_inactive = api_client.post(path, json=body, headers=headers(owner))
    assert still_inactive.status_code == 403


def test_message_persists_in_logical_order_and_is_idempotent(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(api_client, db_session)
    provider = CapturingProvider()
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider

    first = submit(api_client, user, business["id"], "  Keep my original text.  ")
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["owner_message"]["content"] == "  Keep my original text.  "
    assert payload["owner_message"]["sequence_number"] == 1
    assert payload["assistant_message"]["sequence_number"] == 2
    assert payload["replayed"] is False

    repeated = submit(api_client, user, business["id"], "  Keep my original text.  ")
    assert repeated.status_code == 200
    assert (
        repeated.json()["assistant_message"]["id"] == payload["assistant_message"]["id"]
    )
    assert repeated.json()["replayed"] is True
    assert db_session.scalar(select(func.count()).select_from(OwnerConversation)) == 1
    assert db_session.scalar(select(func.count()).select_from(OwnerChatMessage)) == 2
    assert len(provider.requests) == 1

    conflict = submit(api_client, user, business["id"], "Different", key="turn-1")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


@pytest.mark.parametrize(
    ("content", "expected"),
    [(" ", 422), ("x", 200), ("x" * 4_000, 200), ("x" * 4_001, 422)],
)
def test_owner_message_length_boundaries(
    api_client: TestClient,
    db_session: Session,
    content: str,
    expected: int,
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email=f"length-{len(content)}@example.com",
        name=f"Length Market {len(content)}",
    )
    response = submit(api_client, user, business["id"], content)
    assert response.status_code == expected
    if expected == 422:
        assert response.json()["error"]["code"] == "validation_error"


def test_owner_message_preserves_outer_whitespace_at_trimmed_limit(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email="trimmed-owner-limit@example.com",
        name="Trimmed Owner Limit",
    )
    original = f"  {'x' * 4_000}  "

    response = submit(api_client, user, business["id"], original)

    assert response.status_code == 200, response.text
    assert response.json()["owner_message"]["content"] == original


def test_database_rejects_owner_message_above_4000_characters(
    api_client: TestClient, db_session: Session
) -> None:
    _, business = active_business(
        api_client,
        db_session,
        email="database-owner-limit@example.com",
        name="Database Owner Limit",
    )
    conversation = db_session.scalar(
        select(OwnerConversation).where(
            OwnerConversation.business_id == uuid.UUID(str(business["id"]))
        )
    )
    assert conversation is not None
    db_session.add(
        OwnerChatMessage(
            conversation_id=conversation.id,
            sequence_number=1,
            role=ChatMessageRole.OWNER,
            content="x" * 4_001,
            idempotency_key="database-owner-limit",
            generation_state="pending",
        )
    )

    with pytest.raises(IntegrityError, match="content_length"):
        db_session.commit()


def test_database_allows_assistant_message_above_4000_characters(
    api_client: TestClient, db_session: Session
) -> None:
    _, business = active_business(
        api_client,
        db_session,
        email="database-assistant-allowed@example.com",
        name="Database Assistant Allowed",
    )
    conversation = db_session.scalar(
        select(OwnerConversation).where(
            OwnerConversation.business_id == uuid.UUID(str(business["id"]))
        )
    )
    assert conversation is not None
    owner = OwnerChatMessage(
        conversation_id=conversation.id,
        sequence_number=1,
        role=ChatMessageRole.OWNER,
        content="Owner question",
        idempotency_key="database-assistant-allowed",
        generation_state="completed",
    )
    db_session.add(owner)
    db_session.flush()
    assistant = OwnerChatMessage(
        conversation_id=conversation.id,
        sequence_number=2,
        role=ChatMessageRole.ASSISTANT,
        content="a" * 10_000,
        reply_to_message_id=owner.id,
    )
    db_session.add(assistant)

    db_session.commit()

    assert len(assistant.content) == 10_000


def test_database_rejects_assistant_message_above_14000_characters(
    api_client: TestClient, db_session: Session
) -> None:
    _, business = active_business(
        api_client,
        db_session,
        email="database-assistant-limit@example.com",
        name="Database Assistant Limit",
    )
    conversation = db_session.scalar(
        select(OwnerConversation).where(
            OwnerConversation.business_id == uuid.UUID(str(business["id"]))
        )
    )
    assert conversation is not None
    owner = OwnerChatMessage(
        conversation_id=conversation.id,
        sequence_number=1,
        role=ChatMessageRole.OWNER,
        content="Owner question",
        idempotency_key="database-assistant-limit",
        generation_state="completed",
    )
    db_session.add(owner)
    db_session.flush()
    db_session.add(
        OwnerChatMessage(
            conversation_id=conversation.id,
            sequence_number=2,
            role=ChatMessageRole.ASSISTANT,
            content="a" * 14_001,
            reply_to_message_id=owner.id,
        )
    )

    with pytest.raises(IntegrityError, match="content_length"):
        db_session.commit()


def test_provider_failure_keeps_one_owner_message_and_retry_reuses_it(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(api_client, db_session)
    app.dependency_overrides[get_owner_chat_provider] = lambda: TimeoutProvider()
    failed = submit(api_client, user, business["id"], "Please remember this")
    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "assistant_unavailable"
    messages = db_session.scalars(select(OwnerChatMessage)).all()
    assert len(messages) == 1
    assert messages[0].role == ChatMessageRole.OWNER

    app.dependency_overrides[get_owner_chat_provider] = lambda: CapturingProvider()
    retried = submit(api_client, user, business["id"], "Please remember this")
    assert retried.status_code == 200
    assert db_session.scalar(select(func.count()).select_from(OwnerChatMessage)) == 2


def test_ollama_connection_failure_is_safe_and_retry_is_idempotent(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email="ollama-failure@example.com",
        name="Ollama Failure Market",
    )

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = OllamaOwnerChatProvider(
        base_url="http://ollama.invalid",
        model="qwen2.5:7b",
        timeout_seconds=120,
        transport=httpx.MockTransport(unavailable),
    )
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider

    failed = submit(
        api_client, user, business["id"], "Use the local model", key="ollama-retry"
    )

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "assistant_unavailable"
    messages = db_session.scalars(select(OwnerChatMessage)).all()
    assert len(messages) == 1
    assert messages[0].role == ChatMessageRole.OWNER

    app.dependency_overrides[get_owner_chat_provider] = lambda: CapturingProvider()
    retried = submit(
        api_client, user, business["id"], "Use the local model", key="ollama-retry"
    )
    assert retried.status_code == 200
    assert db_session.scalar(select(func.count()).select_from(OwnerChatMessage)) == 2


def test_mocked_ollama_answers_saturday_hours_from_business_schedule(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email="ollama-hours@example.com",
        name="Ollama Hours Market",
    )
    working_hours = valid_hours()
    working_hours[5] = {
        "weekday": "SATURDAY",
        "is_closed": False,
        "shifts": [{"start": "09:00", "end": "14:00"}],
    }
    updated = api_client.patch(
        f"/api/v1/businesses/{business['id']}",
        headers=headers(user),
        json={"working_hours": working_hours},
    )
    assert updated.status_code == 200, updated.text

    def answer(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        system = payload["messages"][0]["content"]
        assert '"weekday":"saturday","is_open":true' in system
        assert '"start":"09:00","end":"14:00"' in system
        content = {
            "reply": "Your business is open on Saturday from 9:00 AM to 2:00 PM.",
            "proposed_knowledge": [],
        }
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": json.dumps(content)}},
        )

    provider = OllamaOwnerChatProvider(
        base_url="http://ollama.invalid",
        model="qwen2.5:7b",
        timeout_seconds=120,
        transport=httpx.MockTransport(answer),
    )
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider

    response = submit(
        api_client,
        user,
        business["id"],
        "What are my business opening hours on Saturday?",
        key="saturday-hours-unique",
    )

    assert response.status_code == 200, response.text
    assert response.json()["assistant_message"]["content"] == (
        "Your business is open on Saturday from 9:00 AM to 2:00 PM."
    )
    assert response.json()["replayed"] is False


def test_context_uses_latest_twelve_messages_and_excludes_expired_knowledge(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(api_client, db_session)
    business_id = uuid.UUID(str(business["id"]))
    conversation = db_session.scalar(
        select(OwnerConversation).where(OwnerConversation.business_id == business_id)
    )
    assert conversation is not None
    conversation.next_turn_number = 8
    for sequence in range(1, 15):
        is_owner = sequence % 2 == 1
        db_session.add(
            OwnerChatMessage(
                conversation_id=conversation.id,
                sequence_number=sequence,
                role=ChatMessageRole.OWNER if is_owner else ChatMessageRole.ASSISTANT,
                content=f"message-{sequence}",
                idempotency_key=f"seed-{sequence}" if is_owner else None,
                generation_state="completed" if is_owner else None,
                reply_to_message_id=(
                    None
                    if is_owner
                    else db_session.scalar(
                        select(OwnerChatMessage.id).where(
                            OwnerChatMessage.conversation_id == conversation.id,
                            OwnerChatMessage.sequence_number == sequence - 1,
                        )
                    )
                ),
            )
        )
        db_session.flush()
    db_session.add_all(
        [
            BusinessKnowledge(
                business_id=business_id,
                subject_key="return_policy",
                content="Returns accepted within seven days",
                kind="permanent",
                category="returns",
            ),
            BusinessKnowledge(
                business_id=business_id,
                subject_key="expired_notice",
                content="Closed yesterday",
                kind="temporary",
                category="temporary_notice",
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            ),
        ]
    )
    db_session.commit()
    provider = CapturingProvider()
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider

    response = submit(
        api_client, user, business_id, "newest-owner-message", key="new-turn"
    )
    assert response.status_code == 200, response.text
    request = provider.requests[0]
    assert len(request.messages) == 12
    assert request.messages[0].content == "message-4"
    assert request.messages[-1].content == "newest-owner-message"
    assert [fact.subject_key for fact in request.knowledge] == ["return_policy"]


def test_history_pages_are_stable_and_tenant_isolated(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(api_client, db_session)
    business_id = uuid.UUID(str(business["id"]))
    conversation = db_session.scalar(
        select(OwnerConversation).where(OwnerConversation.business_id == business_id)
    )
    assert conversation is not None
    conversation.next_turn_number = 31
    owners: dict[int, uuid.UUID] = {}
    same_time = datetime.now(UTC)
    for sequence in range(1, 61):
        is_owner = sequence % 2 == 1
        message = OwnerChatMessage(
            conversation_id=conversation.id,
            sequence_number=sequence,
            role="owner" if is_owner else "assistant",
            content=f"history-{sequence}",
            idempotency_key=f"history-key-{sequence}" if is_owner else None,
            generation_state="completed" if is_owner else None,
            reply_to_message_id=None if is_owner else owners[sequence - 1],
            created_at=same_time,
        )
        db_session.add(message)
        db_session.flush()
        if is_owner:
            owners[sequence] = message.id
    db_session.commit()

    path = f"/api/v1/businesses/{business_id}/owner-chat/messages"
    first = api_client.get(path, headers=headers(user))
    assert first.status_code == 200
    assert len(first.json()["items"]) == 50
    assert [item["sequence_number"] for item in first.json()["items"]] == list(
        range(11, 61)
    )
    second = api_client.get(
        path,
        headers=headers(user),
        params={"cursor": first.json()["next_cursor"]},
    )
    assert [item["sequence_number"] for item in second.json()["items"]] == list(
        range(1, 11)
    )
    assert second.json()["next_cursor"] is None

    foreign = create_user(db_session, "history-foreign@example.com")
    assert api_client.get(path, headers=headers(foreign)).status_code == 404


def test_learned_knowledge_lifecycle_duplicate_update_and_management(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(api_client, db_session)
    first = submit(
        api_client,
        user,
        business["id"],
        "Our delivery charge is 5 USD.",
    )
    assert first.status_code == 200
    path = f"/api/v1/businesses/{business['id']}/knowledge"
    listed = api_client.get(path, headers=headers(user))
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    fact = listed.json()[0]
    assert fact["subject_key"] == "delivery_charge"
    assert fact["kind"] == "permanent"
    created_at = fact["created_at"]

    updated_by_chat = submit(
        api_client,
        user,
        business["id"],
        "Our delivery charge is 7 USD.",
        key="turn-2",
    )
    assert updated_by_chat.status_code == 200
    repeated_fact = api_client.get(path, headers=headers(user)).json()[0]
    assert repeated_fact["id"] == fact["id"]
    assert repeated_fact["content"] == "7 USD"
    assert repeated_fact["created_at"] == created_at

    expiry = datetime.now(UTC) + timedelta(days=1)
    edited = api_client.patch(
        f"{path}/{fact['id']}",
        headers=headers(user),
        json={
            "subject_key": "delivery_notice",
            "content": "Delivery is unavailable tomorrow",
            "kind": "temporary",
            "category": "temporary_notice",
            "expires_at": expiry.isoformat(),
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["kind"] == "temporary"

    invalid_permanent_expiry = api_client.patch(
        f"{path}/{fact['id']}",
        headers=headers(user),
        json={"kind": "permanent", "expires_at": expiry.isoformat()},
    )
    assert invalid_permanent_expiry.status_code == 422

    deleted = api_client.delete(f"{path}/{fact['id']}", headers=headers(user))
    assert deleted.status_code == 204
    assert api_client.get(path, headers=headers(user)).json() == []


def test_temporary_today_and_forbidden_operational_fact_rules(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(api_client, db_session)
    today = submit(
        api_client,
        user,
        business["id"],
        "We close early today at 4 PM.",
    )
    assert today.status_code == 200
    records = db_session.scalars(select(BusinessKnowledge)).all()
    assert len(records) == 1
    assert records[0].expires_at is not None
    assert records[0].expires_at > datetime.now(UTC)

    operational = submit(
        api_client,
        user,
        business["id"],
        "Current stock is 12 units.",
        key="operational",
    )
    assert operational.status_code == 200
    assert db_session.scalar(select(func.count()).select_from(BusinessKnowledge)) == 1


def test_invalid_provider_facts_are_ignored_without_losing_reply(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(api_client, db_session)
    provider = CapturingProvider(
        OwnerChatResult(
            reply="Safe answer.",
            proposed_knowledge=(
                ProposedKnowledge(
                    subject_key="stock",
                    content="50 units",
                    kind="permanent",
                    category="live_operational",
                ),
                ProposedKnowledge(
                    subject_key="ambiguous_notice",
                    content="Closed soon",
                    kind="temporary",
                    category="temporary_notice",
                ),
            ),
        )
    )
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    response = submit(api_client, user, business["id"], "Test invalid proposals")
    assert response.status_code == 200
    assert response.json()["assistant_message"]["content"] == "Safe answer."
    assert db_session.scalar(select(func.count()).select_from(BusinessKnowledge)) == 0


def test_cross_tenant_knowledge_resource_is_privacy_safe(
    api_client: TestClient, db_session: Session
) -> None:
    owner, business = active_business(api_client, db_session)
    submit(api_client, owner, business["id"], "Our return policy is seven days.")
    fact = db_session.scalar(select(BusinessKnowledge))
    assert fact is not None
    foreign, foreign_business = active_business(
        api_client,
        db_session,
        email="knowledge-foreign@example.com",
        name="Foreign Knowledge Market",
    )
    foreign_path = f"/api/v1/businesses/{foreign_business['id']}/knowledge/{fact.id}"
    assert (
        api_client.patch(
            foreign_path,
            headers=headers(foreign),
            json={"content": "Attempted overwrite"},
        ).status_code
        == 404
    )
    assert api_client.delete(foreign_path, headers=headers(foreign)).status_code == 404


def test_assistant_persistence_failure_never_returns_unsaved_reply(
    api_client: TestClient, db_session: Session, migration_engine: Engine
) -> None:
    user, business = active_business(api_client, db_session)
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                """
            CREATE FUNCTION test_reject_assistant() RETURNS trigger AS $$
            BEGIN
                IF NEW.role = 'assistant' THEN
                    RAISE EXCEPTION 'test assistant rejection' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER test_reject_assistant_message
            BEFORE INSERT ON owner_chat_messages
            FOR EACH ROW EXECUTE FUNCTION test_reject_assistant();
            """
            )
        )
    try:
        response = submit(api_client, user, business["id"], "Persist this safely")
        assert response.status_code == 503
        messages = db_session.scalars(select(OwnerChatMessage)).all()
        assert len(messages) == 1
        assert messages[0].role == ChatMessageRole.OWNER
        assert messages[0].generation_state == "failed"
    finally:
        db_session.rollback()
        with migration_engine.begin() as connection:
            connection.execute(
                text(
                    "DROP TRIGGER IF EXISTS test_reject_assistant_message "
                    "ON owner_chat_messages; "
                    "DROP FUNCTION IF EXISTS test_reject_assistant()"
                )
            )

    retried = submit(api_client, user, business["id"], "Persist this safely")
    assert retried.status_code == 200
    assert db_session.scalar(select(func.count()).select_from(OwnerChatMessage)) == 2


class OrderedBlockingProvider:
    def __init__(self) -> None:
        self.first_started = Event()
        self.release_first = Event()
        self.lock = Lock()
        self.requests: list[OwnerChatRequest] = []

    def generate(self, request: OwnerChatRequest) -> OwnerChatResult:
        with self.lock:
            self.requests.append(request)
            call_number = len(self.requests)
        if call_number == 1:
            self.first_started.set()
            assert self.release_first.wait(timeout=5)
        return OwnerChatResult(reply=f"ordered-reply-{call_number}")


def test_simultaneous_same_conversation_turns_generate_in_order(
    api_client: TestClient, db_session: Session, database_engine: Engine
) -> None:
    user, business = active_business(api_client, db_session)
    user_id = user.id
    business_id = uuid.UUID(str(business["id"]))
    provider = OrderedBlockingProvider()
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)

    def attempt(key: str, content: str) -> object:
        with factory() as session:
            thread_user = session.get(User, user_id)
            assert thread_user is not None
            return submit_owner_message(
                session,
                thread_user,
                business_id,
                OwnerMessageRequest(idempotency_key=key, content=content),
                provider,
                get_settings(),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(attempt, "concurrent-1", "first concurrent owner")
        assert provider.first_started.wait(timeout=5)
        second = executor.submit(attempt, "concurrent-2", "second concurrent owner")
        time.sleep(0.1)
        provider.release_first.set()
        first_result = first.result(timeout=10)
        second_result = second.result(timeout=10)

    assert first_result.assistant_message.content == "ordered-reply-1"
    assert second_result.assistant_message.content == "ordered-reply-2"
    assert len(provider.requests) == 2
    assert [message.content for message in provider.requests[0].messages] == [
        "first concurrent owner"
    ]
    assert [message.content for message in provider.requests[1].messages] == [
        "first concurrent owner",
        "ordered-reply-1",
        "second concurrent owner",
    ]
    messages = db_session.scalars(
        select(OwnerChatMessage).order_by(OwnerChatMessage.sequence_number)
    ).all()
    assert [message.sequence_number for message in messages] == [1, 2, 3, 4]


class ParallelBusinessProvider:
    def __init__(self) -> None:
        self.barrier = Barrier(2)

    def generate(self, request: OwnerChatRequest) -> OwnerChatResult:
        self.barrier.wait(timeout=5)
        return OwnerChatResult(reply="parallel reply")


def test_different_businesses_do_not_block_generation(
    api_client: TestClient, db_session: Session, database_engine: Engine
) -> None:
    first_user, first_business = active_business(
        api_client,
        db_session,
        email="parallel-one@example.com",
        name="Parallel One",
    )
    second_user, second_business = active_business(
        api_client,
        db_session,
        email="parallel-two@example.com",
        name="Parallel Two",
    )
    provider = ParallelBusinessProvider()
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)

    def attempt(user_id: uuid.UUID, business_id: str, key: str) -> object:
        with factory() as session:
            user = session.get(User, user_id)
            assert user is not None
            return submit_owner_message(
                session,
                user,
                uuid.UUID(business_id),
                OwnerMessageRequest(idempotency_key=key, content="parallel message"),
                provider,
                get_settings(),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda arguments: attempt(*arguments),
                [
                    (first_user.id, str(first_business["id"]), "parallel-1"),
                    (second_user.id, str(second_business["id"]), "parallel-2"),
                ],
            )
        )
    assert [result.assistant_message.content for result in results] == [
        "parallel reply",
        "parallel reply",
    ]


def test_idempotency_uniqueness_race_creates_one_turn(
    api_client: TestClient, db_session: Session, database_engine: Engine
) -> None:
    user, business = active_business(api_client, db_session)
    user_id = user.id
    business_id = uuid.UUID(str(business["id"]))
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)
    provider = CapturingProvider()
    barrier = Barrier(2)

    def attempt() -> object:
        with factory() as session:
            thread_user = session.get(User, user_id)
            assert thread_user is not None
            barrier.wait()
            return submit_owner_message(
                session,
                thread_user,
                business_id,
                OwnerMessageRequest(
                    idempotency_key="same-race-key", content="same race content"
                ),
                provider,
                get_settings(),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    assert results[0].assistant_message.id == results[1].assistant_message.id
    assert db_session.scalar(select(func.count()).select_from(OwnerConversation)) == 1
    assert db_session.scalar(select(func.count()).select_from(OwnerChatMessage)) == 2
    assert len(provider.requests) == 1
