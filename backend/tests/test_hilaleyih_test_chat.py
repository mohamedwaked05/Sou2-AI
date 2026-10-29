"""Normal HTTP owner turns with synthetic source fixtures and no model calls."""

import uuid
from types import SimpleNamespace

from app.agent.owner_chat_provider import get_owner_chat_provider
from app.core.config import get_settings
from app.database.models import ToolCallLog
from app.integrations.hilaleyih_test import SOURCE_ID, stable_quantity
from app.main import app
from app.schemas.source_mapping import CatalogueProduct, CatalogueResult
from app.services import hilaleyih_test as demo
from pydantic import SecretStr
from sqlalchemy import select

from tests.test_owner_chat import TimeoutProvider, active_business, submit


def test_seven_acceptance_questions_use_persisted_authenticated_owner_path(
    api_client,
    db_session,
    monkeypatch,
):
    user, business = active_business(api_client, db_session)
    monkeypatch.setattr(demo, "BUSINESS_ID", str(business["id"]))
    settings = get_settings().model_copy(
        update={
            "environment": "development",
            "hilaleyih_test_mode": True,
            "tool_call_audit_hmac_secret": SecretStr("synthetic-audit-secret"),
            "development_owner_chat_minute_limit": 120,
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_owner_chat_provider] = lambda: TimeoutProvider()
    product = CatalogueProduct(
        external_product_id="synthetic-pepsi-15",
        names=("Pepsi Bottle 1.5 L",),
        sku=None,
        categories=(),
    )
    source = SimpleNamespace(
        id=uuid.UUID(SOURCE_ID),
        connection_profile_key="hila_catalogue_dev",
        status=demo.OperationalDataSourceStatus.ACTIVE,
    )

    class Executor:
        def __init__(self, *_args):
            pass

        def _active_source(self, _business):
            return source

        def catalogue_scope(self, _source):
            return SimpleNamespace(
                mapping_version=5,
                source_fingerprint="a" * 64,
                schema_fingerprint="b" * 64,
            )

        def execute(self, *, arguments, tool_name, **_kwargs):
            if tool_name == "operational_report":
                return SimpleNamespace(
                    output=SimpleNamespace(
                        status="ok",
                        totals=[SimpleNamespace(transaction_count=5815)],
                    )
                )
            if arguments.get("query") == "pepsi":
                return SimpleNamespace(
                    output=CatalogueResult(
                        status="ambiguous",
                        items=(
                            product,
                            product.model_copy(
                                update={
                                    "external_product_id": "synthetic-pepsi-05",
                                    "names": ("Pepsi Bottle 0.5 L",),
                                }
                            ),
                        ),
                        truncated=False,
                        mapping_version=5,
                    )
                )
            if arguments.get("query") == "nonexistent":
                return SimpleNamespace(
                    output=CatalogueResult(
                        status="not_found",
                        items=(),
                        truncated=False,
                        mapping_version=5,
                    )
                )
            return SimpleNamespace(
                output=CatalogueResult(
                    status="resolved",
                    items=(product,),
                    truncated=False,
                    mapping_version=5,
                    matched_records=9343,
                )
            )

    class Reader:
        def __init__(self, *_args):
            pass

        def price(self, product_id):
            assert product_id == product.external_product_id
            return {"Selling1": 1234, "ItemCurr": "unverified", "Unit": None}

        def financial_summary(self, *_args):
            return [
                dict(
                    currency_code="unverified",
                    documents=5815,
                    gross=100,
                    discounts=10,
                    taxes=2,
                    signed_total=92,
                    negative_documents=2,
                    negative_total=-5,
                    settlement_discrepancies=1,
                    settlement_difference=1,
                    incomplete_documents=0,
                )
            ]

    monkeypatch.setattr(demo, "OperationalToolExecutor", Executor)
    monkeypatch.setattr(demo, "HilaleyihHistoricalReader", Reader)
    monkeypatch.setattr(demo, "connector_for", lambda *_args: object())
    questions = [
        ("What is the price of Pepsi Bottle 1.5 L?", "source amount 1234"),
        ("How much does this product cost?", "source amount 1234"),
        ("How many Pepsi bottles are in stock?", "DEMO stock"),
        ("Check stock in the Jbeil branch.", "Jbeil test location"),
        ("How many products are in our catalogue?", "9,343"),
        ("How many Paid SI sales documents were recorded in February 2023?", "5,815"),
        ("Give me historical revenue for February 2023.", "= 92"),
    ]
    for index, (question, expected) in enumerate(questions):
        response = submit(api_client, user, business["id"], question, key=str(index))
        assert response.status_code == 200, response.text
        answer = response.json()["assistant_message"]["content"]
        assert expected in answer, answer
        if index == 3:
            assert str(stable_quantity(product.external_product_id, "jbeil")) in answer
        replay = submit(api_client, user, business["id"], question, key=str(index))
        assert (
            replay.json()["assistant_message"]["id"]
            == response.json()["assistant_message"]["id"]
        )
    assert len(db_session.scalars(select(ToolCallLog)).all()) == 7
    for index, (question, expected) in enumerate(
        [
            ("How many Pepsi bottles are in stock?", "Which catalogue variant"),
            ("1", "DEMO stock"),
            ("Check stock in the Jbeil branch.", "Jbeil test location"),
            ("Check stock in the Unknown branch.", "Choose a DEMO test location"),
            ("What is the price of nonexistent?", "No matching valid catalogue"),
            ("How much does this product cost?", "Which catalogue product"),
        ]
    ):
        response = submit(
            api_client, user, business["id"], question, key=f"extra-{index}"
        )
        assert response.status_code == 200, response.text
        assert expected in response.json()["assistant_message"]["content"]

    # The same route still requires authentication even with test mode enabled.
    assert (
        api_client.post(
            f"/api/v1/businesses/{business['id']}/owner-chat/messages",
            json={"content": questions[0][0], "idempotency_key": "unauthorized"},
        ).status_code
        == 401
    )
