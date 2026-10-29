"""Oct 29 exact-name, identifier, clarification and single-store failures."""

import re
import uuid
from types import SimpleNamespace

import pytest
from app.database.models import OperationalDataSourceStatus
from app.integrations.hilaleyih_test import BUSINESS_ID, SOURCE_ID
from app.schemas.source_mapping import CatalogueProduct, CatalogueResult
from app.services import hilaleyih_test as chat
from pydantic import SecretStr


@pytest.fixture
def conversation(monkeypatch):
    products = (
        CatalogueProduct(
            external_product_id="1000000000001",
            names=("PEPSI 1.25L",),
            sku=None,
            categories=(),
        ),
        CatalogueProduct(
            external_product_id="1000000000002",
            names=("PEPSI DIET 1.25L",),
            sku=None,
            categories=(),
        ),
        CatalogueProduct(
            external_product_id="1000000000003",
            names=("PEPSI 2.25L",),
            sku=None,
            categories=(),
        ),
    )
    searches = []

    class Executor:
        def __init__(self, *_args):
            pass

        def _active_source(self, _business):
            return SimpleNamespace(
                id=uuid.UUID(SOURCE_ID),
                connection_profile_key="hila_catalogue_dev",
                status=OperationalDataSourceStatus.ACTIVE,
            )

        def catalogue_scope(self, _source):
            return SimpleNamespace(
                mapping_version=5,
                source_fingerprint="a" * 64,
                schema_fingerprint="b" * 64,
            )

        def execute(self, *, arguments, **_kwargs):
            searches.append(arguments)
            if "external_product_id" in arguments:
                found = [
                    p
                    for p in products
                    if p.external_product_id == arguments["external_product_id"]
                ]
            else:
                query = arguments["query"].replace(" ", "").lower()
                terms = re.findall(r"\S+", arguments["query"].lower())
                found = [
                    p
                    for p in products
                    if query == p.external_product_id
                    or all(t in p.names[0].replace(" ", "").lower() for t in terms)
                ]
            return SimpleNamespace(
                output=CatalogueResult(
                    items=tuple(found),
                    status="not_found"
                    if not found
                    else "resolved"
                    if len(found) == 1
                    else "ambiguous",
                    truncated=False,
                    mapping_version=5,
                )
            )

    class Reader:
        def __init__(self, *_args):
            pass

        def price(self, _product):
            return {"Selling1": 75000, "ItemCurr": "1", "Unit": "pc"}

        def inventory(self, _product):
            return dict(
                unit="pc",
                first_date="2023-01-02",
                last_date="2023-02-16",
                calendar_days=46,
                line_count=276,
                usable_lines=275,
                demand_quantity=288,
                negative_lines=1,
                negative_quantity=-2,
                unusable_lines=0,
                orphan_lines=0,
                records=["SI/80/0"],
                balance=None,
                balance_reason="No reliable matched historical balance.",
            )

    monkeypatch.setattr(chat, "OperationalToolExecutor", Executor)
    monkeypatch.setattr(chat, "HilaleyihHistoricalReader", Reader)
    monkeypatch.setattr(chat, "connector_for", lambda *_args: object())
    monkeypatch.setattr(chat, "load_full_access_business", lambda *_args: None)
    user = SimpleNamespace(id=uuid.uuid4())
    settings = SimpleNamespace(
        environment="development",
        hilaleyih_test_mode=True,
        tool_call_audit_hmac_secret=SecretStr("synthetic-test-audit"),
    )
    session = SimpleNamespace(
        scalar=lambda *_args: session.previous, previous=None, add=lambda *_args: None
    )

    def turn(text):
        owner = SimpleNamespace(
            id=uuid.uuid4(),
            conversation_id=uuid.UUID(int=3),
            sequence_number=1
            if session.previous is None
            else session.previous.sequence_number + 2,
            content=text,
            operational_clarification=None,
        )
        result = chat.answer(
            session, uuid.UUID(BUSINESS_ID), user, owner, settings, object()
        )
        session.previous = owner
        return result.reply if result else "NOT_HANDLED"

    turn.session = session
    return turn, searches


def test_repeated_exact_non_diet_names_never_reopen_diet_choices(conversation):
    turn, _ = conversation
    assert "Which catalogue variant" in turn("Price of Pepsi?")
    for question in (
        "PEPSI 1.25L",
        "What is the price of PEPSI 1.25L?",
        "How much does this product cost?",
    ):
        reply = turn(question)
        assert "source amount 75000" in reply and "DIET" not in reply


def test_natural_bottle_and_spaced_unit_resolves_existing_identity(conversation):
    turn, _ = conversation
    assert "source amount 75000" in turn("What is the price of Pepsi Bottle 1.25 L?")


@pytest.mark.parametrize("selected", [False, True])
def test_pre_fix_saved_context_resumes_the_original_operation(conversation, selected):
    turn, _ = conversation
    turn("Price of Pepsi?")
    if selected:
        turn("1")
    old = turn.session.previous.operational_clarification
    old.pop("candidate_names")
    if selected:
        old.pop("candidates")
    old["location"] = "jbeil"
    assert "source amount 75000" in turn("PEPSI 1.25L")
    reply = turn("How many are in stock?")
    assert "44 pc DEMO stock proxy" in reply
    assert "jbeil" not in reply.lower()


@pytest.mark.parametrize(
    "selection", ["1", "PEPSI 1.25L", "1000000000001", "product barcode 1000000000001"]
)
def test_offered_selection_and_followup_preserve_narrowed_product(
    conversation, selection
):
    turn, _ = conversation
    assert "Which catalogue variant" in turn("Price of Pepsi?")
    assert "source amount 75000" in turn(selection)
    assert "source amount 75000" in turn("How much does this product cost?")


@pytest.mark.parametrize("question", ["1000000000001", "product barcode 1000000000001"])
def test_identifier_only_uses_authorized_search_and_retains_price_context(
    conversation, question
):
    turn, searches = conversation
    reply = turn(question)
    assert "PEPSI 1.25L" in reply and "DIET" not in reply
    assert any(x.get("query") == "1000000000001" for x in searches)
    assert "75000" in turn("How much does this product cost?")


@pytest.mark.parametrize(
    "question",
    [
        "product barcode 9999999999999",
        "What is the price of product barcode 9999999999999?",
    ],
)
def test_unknown_barcode_is_not_invented(conversation, question):
    turn, _ = conversation
    reply = turn(question)
    assert "identifier" in reply.lower() and "not" in reply.lower()


def test_observed_oct29_wording_keeps_one_non_diet_product(conversation):
    turn, _ = conversation
    for question in (
        "What is the price of Pepsi Bottle 1.25 L?",
        "What is the price of PEPSI 1.25L?",
        "PEPSI 1.25L",
        "How much does that Pepsi cost?",
        "What is the price of product barcode 1000000000001?",
        "How much does that Pepsi cost?",
    ):
        reply = turn(question)
        assert "source amount 75000" in reply and "DIET" not in reply
    assert "44 pc DEMO stock proxy" in turn("How many Pepsi bottles are in stock?")
    for _ in range(2):
        assert "has no branches" in turn("Check its stock in the Jbeil branch.")
    assert "44 pc DEMO stock proxy" in turn("Check its stock.")


@pytest.mark.parametrize(
    "question",
    [
        "Check stock in the Unknown branch.",
        "Does Hilaleyih have branches?",
    ],
)
def test_no_branch_names_can_produce_branch_quantities(conversation, question):
    turn, _ = conversation
    reply = turn(question)
    assert "has no branches" in reply and "DEMO stock" not in reply


def test_jbeil_is_rejected_without_losing_selected_store_product(conversation):
    turn, _ = conversation
    turn("Price of Pepsi?")
    turn("1")
    reply = turn("Check stock in the Jbeil branch.")
    assert "no branches" in reply.lower() and "DEMO stock" not in reply
    reply = turn("How many are in stock?")
    assert "Hilaleyih Market" in reply and "44" in reply
    assert "288" in reply and "46" in reply and "7" in reply
    assert "confidence: low" in reply.lower()


def test_stock_with_no_product_quantity_evidence_has_no_number(
    monkeypatch, conversation
):
    turn, _ = conversation
    monkeypatch.setattr(
        chat.HilaleyihHistoricalReader,
        "inventory",
        lambda *_args: dict(
            unit=None,
            first_date=None,
            last_date=None,
            calendar_days=0,
            line_count=0,
            usable_lines=0,
            demand_quantity=0,
            negative_lines=0,
            negative_quantity=0,
            unusable_lines=0,
            orphan_lines=0,
            records=[],
            balance=None,
            balance_reason="No matched balance.",
        ),
    )
    reply = turn("Stock of PEPSI 1.25L?")
    assert "no evidence-based numerical estimate" in reply.lower()
