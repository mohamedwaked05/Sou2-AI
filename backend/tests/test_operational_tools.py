"""Controlled registry, executor, audit, and owner-loop coverage."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event
from typing import Any, cast

import pytest
from app.agent.owner_chat_provider import (
    OwnerChatProviderTimeout,
    OwnerChatRequest,
    OwnerChatResult,
    TokenUsage,
    get_owner_chat_provider,
)
from app.core.config import Settings, get_settings
from app.database.models import (
    Business,
    BusinessStatus,
    OperationalDataSourceConfig,
    OperationalDataSourceStatus,
    OwnerChatCitation,
    ToolCallLog,
    ToolCallStatus,
    User,
)
from app.integrations.operational import OperationalQueryTimeout
from app.integrations.profiles import (
    FAKE_STORE_MAPPING,
    FAKE_STORE_PROFILE,
    ConnectionProfileRegistry,
    get_connection_profile_registry,
)
from app.main import app
from app.schemas.operational import (
    BestSellingProduct,
    BestSellingProductsResult,
    CategoryCandidate,
    CategoryResolution,
    IntegrationHealth,
    InventoryItem,
    InventoryResult,
    OperationalResultMetadata,
    Product,
    ProductResolution,
    ProductResolutionCandidate,
    ReportingPeriod,
    RestockingRecommendation,
    RestockingRecommendationsResult,
    SalesSummary,
)
from app.schemas.owner_chat import OwnerMessageRequest
from app.services import owner_chat
from app.services.owner_chat import submit_owner_message
from app.tools.operational import (
    BEST_SELLING_PRODUCTS_TOOL,
    CURRENT_INVENTORY_TOOL,
    RESTOCKING_RECOMMENDATIONS_TOOL,
    SALES_SUMMARY_TOOL,
    OperationalToolExecutor,
    ToolExecutionError,
    build_operational_tool_registry,
)
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from tests.test_data_sources import create_owner_business
from tests.test_owner_chat import active_business, submit

NOW = datetime(2026, 8, 24, 6, tzinfo=UTC)


def audit_settings() -> Settings:
    return get_settings().model_copy(
        update={"tool_call_audit_hmac_secret": SecretStr("test-only-tool-audit-secret")}
    )


def metadata(*, limit: int | None = None, rows: int = 1) -> OperationalResultMetadata:
    return OperationalResultMetadata(
        source_timezone="Asia/Beirut",
        data_timestamp=NOW,
        queried_at=NOW,
        freshness_seconds=0,
        row_count=rows,
        requested_limit=limit,
    )


def inventory_item() -> InventoryItem:
    return InventoryItem(
        product=Product(
            external_product_id="P1001",
            sku="RICE-5KG",
            barcode="5280001000011",
            name="Lebanese Rice 5kg",
            category="Pantry",
        ),
        branch_external_id="BR-BEY",
        branch_name="Beirut Branch",
        on_hand_quantity=Decimal("10"),
        reserved_quantity=Decimal("2"),
        available_quantity=Decimal("8"),
        reorder_point=Decimal("9"),
        target_stock=Decimal("20"),
    )


class StubSource:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.resolution_references: list[str] = []
        self.last_inventory_query: Any | None = None
        self.last_restocking_query: Any | None = None
        self.error: Exception | None = None
        self.health_error: Exception | None = None
        self.categories = (
            CategoryCandidate(external_category_id="category-1", label="Pantry"),
        )
        self.timeout_seconds = 2
        self.resolution = ProductResolution(
            status="resolved",
            matched_by="alias",
            product=inventory_item().product,
            metadata=metadata(),
        )

    @property
    def enforced_query_timeout_seconds(self) -> int:
        return self.timeout_seconds

    def resolve_product(self, query: object) -> ProductResolution:
        self.resolution_references.append(cast(Any, query).reference)
        return self.resolution

    def resolve_category(self, query: object) -> CategoryResolution:
        return CategoryResolution(
            status="resolved",
            category=CategoryCandidate(
                external_category_id="category-1", label="Pantry"
            ),
            metadata=metadata(),
        )

    def list_categories(self, *, limit: int) -> tuple[CategoryCandidate, ...]:
        return self.categories[:limit]

    def check_health(self) -> IntegrationHealth:
        if self.health_error is not None:
            raise self.health_error
        return IntegrationHealth(
            status="healthy",
            checked_at=NOW,
            source_timezone="Asia/Beirut",
            currency="LBP",
            data_timestamp=NOW,
        )

    def _raise_or_record(self, name: str) -> None:
        self.calls.append(name)
        if self.error is not None:
            raise self.error

    def get_current_inventory(self, query: object) -> InventoryResult:
        self._raise_or_record(CURRENT_INVENTORY_TOOL)
        self.last_inventory_query = query
        limit = cast(Any, query).limit
        return InventoryResult(
            items=(inventory_item(),), metadata=metadata(limit=limit)
        )

    def get_sales_summary(self, query: object) -> SalesSummary:
        self._raise_or_record(SALES_SUMMARY_TOOL)
        query = cast(Any, query)
        return SalesSummary(
            period=ReportingPeriod(
                start_date=query.start_date,
                end_date=query.end_date,
                source_timezone="Asia/Beirut",
            ),
            branch_external_id=query.branch_external_id,
            completed_sale_count=2,
            returned_sale_count=1,
            completed_refund_count=1,
            gross_quantity_sold=Decimal("12"),
            returned_quantity=Decimal("2"),
            net_quantity_sold=Decimal("10"),
            gross_revenue=Decimal("2400000"),
            refund_amount=Decimal("400000"),
            net_revenue=Decimal("2000000"),
            currency="LBP",
            metadata=metadata(),
        )

    def get_best_selling_products(self, query: object) -> BestSellingProductsResult:
        self._raise_or_record(BEST_SELLING_PRODUCTS_TOOL)
        query = cast(Any, query)
        return BestSellingProductsResult(
            period=ReportingPeriod(
                start_date=query.start_date,
                end_date=query.end_date,
                source_timezone="Asia/Beirut",
            ),
            branch_external_id=query.branch_external_id,
            items=(
                BestSellingProduct(
                    rank=1,
                    product=inventory_item().product,
                    quantity_sold=Decimal("10"),
                    revenue=Decimal("2000000"),
                    currency="LBP",
                ),
            ),
            metadata=metadata(limit=query.limit),
        )

    def get_restocking_recommendations(
        self, query: object
    ) -> RestockingRecommendationsResult:
        self._raise_or_record(RESTOCKING_RECOMMENDATIONS_TOOL)
        self.last_restocking_query = query
        limit = cast(Any, query).limit
        return RestockingRecommendationsResult(
            items=(
                RestockingRecommendation(
                    inventory=inventory_item(), recommended_quantity=Decimal("12")
                ),
            ),
            metadata=metadata(limit=limit),
        )


class StubRegistry:
    def __init__(self, source: StubSource | None = None) -> None:
        self.source = source or StubSource()

    def available_profiles(self):
        return (FAKE_STORE_PROFILE,)

    def get_profile(self, key: str):
        return FAKE_STORE_PROFILE if key == FAKE_STORE_PROFILE.key else None

    def get_mapping(self, key: str, version: int):
        if key == FAKE_STORE_MAPPING.key and version == FAKE_STORE_MAPPING.version:
            return FAKE_STORE_MAPPING
        return None

    def resolve(self, key: str):
        if key != FAKE_STORE_PROFILE.key:
            raise KeyError
        return self.source


def active_source(
    session: Session, business_id: uuid.UUID
) -> OperationalDataSourceConfig:
    source = OperationalDataSourceConfig(
        business_id=business_id,
        display_name="Operational Demo",
        adapter_type=FAKE_STORE_PROFILE.adapter_type,
        connection_profile_key=FAKE_STORE_PROFILE.key,
        mapping_profile_key=FAKE_STORE_MAPPING.key,
        mapping_profile_version=FAKE_STORE_MAPPING.version,
        status=OperationalDataSourceStatus.ACTIVE,
        last_validated_at=NOW,
        last_successful_health_check_at=NOW,
    )
    session.add(source)
    session.commit()
    return source


def executor_setup(client: TestClient, session: Session):
    user, response = active_business(
        client,
        session,
        email=f"tools-{uuid.uuid4()}@example.com",
        name=f"Tools {uuid.uuid4()}",
    )
    business = session.get(Business, uuid.UUID(str(response["id"])))
    assert business is not None and business.status is BusinessStatus.ACTIVE
    active_source(session, business.id)
    registry = StubRegistry()
    executor = OperationalToolExecutor(
        session,
        cast(ConnectionProfileRegistry, registry),
        audit_settings(),
    )
    return user, business, registry, executor


def test_registry_contains_exactly_four_provider_neutral_tools() -> None:
    registry = build_operational_tool_registry(timeout_seconds=2)

    assert tuple(registry) == (
        CURRENT_INVENTORY_TOOL,
        SALES_SUMMARY_TOOL,
        BEST_SELLING_PRODUCTS_TOOL,
        RESTOCKING_RECOMMENDATIONS_TOOL,
    )
    assert all(item.timeout_seconds == 2 for item in registry.values())
    assert all(item.executor is not None for item in registry.values())
    serialized = str([item.provider_schema() for item in registry.values()]).casefold()
    assert "postgres" not in serialized
    assert "sql" not in serialized
    assert "password" not in serialized


@pytest.mark.parametrize(
    ("tool_name", "arguments", "code"),
    [
        ("run_sql", {}, "unknown_tool"),
        (
            CURRENT_INVENTORY_TOOL,
            {"business_id": str(uuid.uuid4())},
            "invalid_arguments",
        ),
        (CURRENT_INVENTORY_TOOL, {"limit": "5"}, "invalid_arguments"),
        (
            CURRENT_INVENTORY_TOOL,
            {"product_filter": "DROP TABLE products"},
            "invalid_arguments",
        ),
        (
            CURRENT_INVENTORY_TOOL,
            {"product_filter": "https://evil.invalid"},
            "invalid_arguments",
        ),
        (CURRENT_INVENTORY_TOOL, {"password": "guess"}, "invalid_arguments"),
        (
            BEST_SELLING_PRODUCTS_TOOL,
            {"start_date": "2026-08-20", "end_date": "2026-08-21", "limit": 21},
            "result_limit",
        ),
    ],
)
def test_unknown_malformed_and_control_arguments_are_denied_and_audited(
    api_client: TestClient,
    db_session: Session,
    tool_name: str,
    arguments: object,
    code: str,
) -> None:
    user, business, registry, executor = executor_setup(api_client, db_session)

    with pytest.raises(ToolExecutionError) as caught:
        executor.execute(
            user=user,
            business_id=business.id,
            tool_name=tool_name,
            arguments=arguments,
        )

    assert caught.value.code == code
    audit = db_session.scalar(select(ToolCallLog))
    assert audit is not None
    assert audit.status in {ToolCallStatus.DENIED, ToolCallStatus.ERROR}
    assert audit.error_code == code
    assert len(audit.args_hash) == 64
    assert registry.source.calls == []


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_type"),
    [
        (
            CURRENT_INVENTORY_TOOL,
            {"branch_external_id": "BR-BEY", "limit": 5},
            InventoryResult,
        ),
        (
            SALES_SUMMARY_TOOL,
            {"start_date": "2026-08-20", "end_date": "2026-08-23"},
            SalesSummary,
        ),
        (
            BEST_SELLING_PRODUCTS_TOOL,
            {"start_date": "2026-08-20", "end_date": "2026-08-23", "limit": 5},
            BestSellingProductsResult,
        ),
        (
            RESTOCKING_RECOMMENDATIONS_TOOL,
            {"branch_external_id": "BR-BEY", "limit": 5},
            RestockingRecommendationsResult,
        ),
    ],
)
def test_every_approved_tool_executes_normalized_data_and_one_minimal_audit(
    api_client: TestClient,
    db_session: Session,
    tool_name: str,
    arguments: dict[str, object],
    expected_type: type[object],
) -> None:
    user, business, registry, executor = executor_setup(api_client, db_session)

    result = executor.execute(
        user=user,
        business_id=business.id,
        tool_name=tool_name,
        arguments=arguments,
    )

    assert isinstance(result.output, expected_type)
    assert registry.source.calls == [tool_name]
    audit = db_session.scalar(select(ToolCallLog))
    assert audit is not None
    assert audit.status is ToolCallStatus.SUCCESS
    assert audit.error_code is None
    assert audit.latency_ms is not None and audit.latency_ms >= 0
    assert set(inspect(audit).mapper.column_attrs.keys()) == {
        "id",
        "business_id",
        "user_id",
        "tool_name",
        "args_hash",
        "status",
        "error_code",
        "latency_ms",
        "created_at",
    }


@pytest.mark.parametrize(
    "tool_name",
    [CURRENT_INVENTORY_TOOL, RESTOCKING_RECOMMENDATIONS_TOOL],
)
def test_product_scoped_tools_resolve_then_query_the_stable_external_id(
    api_client: TestClient,
    db_session: Session,
    tool_name: str,
) -> None:
    user, business, registry, executor = executor_setup(api_client, db_session)

    result = executor.execute(
        user=user,
        business_id=business.id,
        tool_name=tool_name,
        arguments={"product_filter": "mayyet Nestle", "limit": 5},
    )

    assert cast(Any, result.output).resolution.status == "resolved"
    assert registry.source.resolution_references == ["mayyet Nestle"]
    read_query = (
        registry.source.last_inventory_query
        if tool_name == CURRENT_INVENTORY_TOOL
        else registry.source.last_restocking_query
    )
    assert read_query.external_product_id == "P1001"
    assert not hasattr(read_query, "product_filter")


def test_category_scoped_inventory_resolves_before_read_query(
    api_client: TestClient, db_session: Session
) -> None:
    user, business, registry, executor = executor_setup(api_client, db_session)

    result = executor.execute(
        user=user,
        business_id=business.id,
        tool_name=CURRENT_INVENTORY_TOOL,
        arguments={"category_filter": "Pantry", "limit": 5},
    )

    assert isinstance(result.output, InventoryResult)
    assert registry.source.last_inventory_query.category_filter == "Pantry"
    assert registry.source.resolution_references == []


def test_operational_planner_receives_bounded_source_categories(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client, db_session, email=f"category-plan-{uuid.uuid4()}@example.com"
    )
    source = StubSource()
    source.categories = (
        CategoryCandidate(external_category_id="category-7", label="Beverages"),
        CategoryCandidate(external_category_id="category-8", label="Pantry"),
    )
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"category_filter": "Beverages", "limit": 5},
            ),
            usage_result(reply="There are products in that category."),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)

    response = submit(
        api_client,
        user,
        business["id"],
        "what drinks do we have?",
        f"category-plan-{uuid.uuid4()}",
    )

    assert response.status_code == 200, response.text
    assert [
        candidate.label for candidate in provider.requests[0].category_candidates
    ] == [
        "Beverages",
        "Pantry",
    ]
    assert provider.requests[0].rolling_summary is None


def test_profit_metric_is_rejected_when_mapping_has_no_cost_capability(
    api_client: TestClient, db_session: Session
) -> None:
    user, business, _registry, executor = executor_setup(api_client, db_session)

    with pytest.raises(ToolExecutionError) as caught:
        executor.execute(
            user=user,
            business_id=business.id,
            tool_name=SALES_SUMMARY_TOOL,
            arguments={
                "start_date": "2026-08-20",
                "end_date": "2026-08-23",
                "metric": "gross_profit",
            },
        )

    assert caught.value.code == "capability_unavailable"


@pytest.mark.parametrize(
    ("status", "candidate_count"),
    [("ambiguous", 2), ("not_found", 0)],
)
def test_unresolved_products_never_execute_inventory_for_an_arbitrary_candidate(
    api_client: TestClient,
    db_session: Session,
    status: str,
    candidate_count: int,
) -> None:
    user, business, registry, executor = executor_setup(api_client, db_session)
    candidates = (
        (
            ProductResolutionCandidate(
                external_product_id="P1007", sku="PEPSI-330", name="Pepsi 330 ml"
            ),
            ProductResolutionCandidate(
                external_product_id="P1008",
                sku="PEPSI-1500",
                name="Pepsi 1.5 L",
            ),
        )
        if status == "ambiguous"
        else ()
    )
    registry.source.resolution = ProductResolution(
        status=cast(Any, status),
        matched_by="partial_name" if status == "ambiguous" else None,
        candidates=candidates,
        metadata=metadata(rows=candidate_count),
    )

    result = executor.execute(
        user=user,
        business_id=business.id,
        tool_name=CURRENT_INVENTORY_TOOL,
        arguments={"product_filter": "Pepsi", "limit": 5},
    )

    assert cast(Any, result.output).resolution.status == status
    assert cast(Any, result.output).items == ()
    assert registry.source.calls == []
    assert db_session.scalar(select(ToolCallLog)).status is ToolCallStatus.SUCCESS


def test_executor_requires_a_bounded_adapter_enforced_timeout(
    api_client: TestClient,
    db_session: Session,
) -> None:
    user, business, registry, executor = executor_setup(api_client, db_session)
    registry.source.timeout_seconds = 3

    assert executor.available_definitions(user, business.id) == ()
    with pytest.raises(ToolExecutionError) as raised:
        executor.execute(
            user=user,
            business_id=business.id,
            tool_name=CURRENT_INVENTORY_TOOL,
            arguments={"limit": 5},
        )

    assert raised.value.code == "integration_unavailable"
    assert registry.source.calls == []


def test_cross_tenant_timeout_and_missing_audit_secret_fail_safely(
    api_client: TestClient,
    db_session: Session,
) -> None:
    user, business, registry, executor = executor_setup(api_client, db_session)
    foreign, _foreign_business = create_owner_business(
        db_session, "foreign-tools@example.com", "Foreign Tools"
    )
    with pytest.raises(ToolExecutionError) as denied:
        executor.execute(
            user=foreign,
            business_id=business.id,
            tool_name=CURRENT_INVENTORY_TOOL,
            arguments={"limit": 5},
        )
    assert denied.value.code == "authorization_denied"
    assert db_session.scalar(select(ToolCallLog)).status is ToolCallStatus.DENIED

    db_session.query(ToolCallLog).delete()
    db_session.commit()
    registry.source.error = OperationalQueryTimeout()
    with pytest.raises(ToolExecutionError) as timed_out:
        executor.execute(
            user=user,
            business_id=business.id,
            tool_name=CURRENT_INVENTORY_TOOL,
            arguments={"limit": 5},
        )
    assert timed_out.value.code == "timeout"
    assert db_session.scalar(select(ToolCallLog)).error_code == "timeout"

    db_session.query(ToolCallLog).delete()
    db_session.commit()
    no_secret = OperationalToolExecutor(
        db_session,
        cast(ConnectionProfileRegistry, registry),
        get_settings().model_copy(update={"tool_call_audit_hmac_secret": None}),
    )
    calls_before = list(registry.source.calls)
    with pytest.raises(ToolExecutionError) as unavailable:
        no_secret.execute(
            user=user,
            business_id=business.id,
            tool_name=CURRENT_INVENTORY_TOOL,
            arguments={"limit": 5},
        )
    assert unavailable.value.code == "audit_unavailable"
    assert registry.source.calls == calls_before
    assert db_session.scalar(select(func.count()).select_from(ToolCallLog)) == 0


class SequenceProvider:
    def __init__(self, results: list[OwnerChatResult]) -> None:
        self.results = results
        self.requests: list[OwnerChatRequest] = []

    def estimate_input_tokens(self, request: OwnerChatRequest) -> int:
        return 10

    def generate(self, request: OwnerChatRequest) -> OwnerChatResult:
        self.requests.append(request)
        return self.results[len(self.requests) - 1]


class FailingSecondProvider(SequenceProvider):
    def generate(self, request: OwnerChatRequest) -> OwnerChatResult:
        if self.requests:
            self.requests.append(request)
            raise OwnerChatProviderTimeout(usage_uncertain=True)
        return super().generate(request)


class BlockingOperationalProvider(SequenceProvider):
    def __init__(self, results: list[OwnerChatResult]) -> None:
        super().__init__(results)
        self.started = Event()
        self.release = Event()

    def generate(self, request: OwnerChatRequest) -> OwnerChatResult:
        if not self.requests:
            self.started.set()
            assert self.release.wait(timeout=5)
        return super().generate(request)


def usage_result(**values: object) -> OwnerChatResult:
    return OwnerChatResult(
        usage=TokenUsage(
            input_tokens=10,
            output_tokens=2,
            total_tokens=12,
            authoritative=True,
        ),
        provider_identifier="offline-test",
        model_identifier="sequence",
        **values,
    )


def configure_operational_chat(
    db_session: Session,
    business_id: object,
    source: StubSource,
    provider: SequenceProvider,
) -> None:
    active_source(db_session, uuid.UUID(str(business_id)))
    registry = StubRegistry(source)
    settings = audit_settings()
    app.dependency_overrides[get_connection_profile_registry] = lambda: cast(
        ConnectionProfileRegistry, registry
    )
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider
    app.dependency_overrides[get_settings] = lambda: settings


def test_owner_loop_executes_live_tool_aggregates_usage_and_replays_without_work(
    api_client: TestClient,
    db_session: Session,
    migration_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, business = active_business(api_client, db_session)
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"branch_external_id": "BR-BEY", "limit": 5},
            ),
            usage_result(
                reply="Beirut has 8 available units as of the source timestamp."
            ),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    enqueued: list[uuid.UUID] = []
    monkeypatch.setattr(
        owner_chat,
        "_enqueue_summary_safely",
        lambda conversation_id, _settings: enqueued.append(conversation_id),
    )

    first = submit(
        api_client,
        user,
        business["id"],
        "What is the current inventory in Beirut now?",
        "live-inventory",
    )
    replay = submit(
        api_client,
        user,
        business["id"],
        "What is the current inventory in Beirut now?",
        "live-inventory",
    )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert len(provider.requests) == 2
    assert CURRENT_INVENTORY_TOOL in {tool.name for tool in provider.requests[0].tools}
    assert provider.requests[0].tool_results == ()
    supplied = provider.requests[1].tool_results[0]
    assert supplied.tool_name == CURRENT_INVENTORY_TOOL
    assert supplied.output["items"][0]["available_quantity"] == "8"
    assert "connection_profile" not in str(supplied.output)
    assert source.calls == [CURRENT_INVENTORY_TOOL]
    assert enqueued == []
    assert db_session.scalar(select(func.count()).select_from(ToolCallLog)) == 1
    assert db_session.scalar(select(func.count()).select_from(OwnerChatCitation)) == 0
    with migration_engine.connect() as connection:
        reservation = connection.execute(
            text(
                "SELECT input_tokens, output_tokens, total_tokens "
                "FROM ai_usage_reservations"
            )
        ).one()
    assert reservation.input_tokens == 20
    assert reservation.output_tokens == 4
    assert reservation.total_tokens == 24


@pytest.mark.parametrize(
    ("message", "reference"),
    [
        ("How many Pepsi do we have left?", "Pepsi"),
        ("How many Pepsi we have left?", "how many pepsi we have left"),
        ("قديش عنا بيبسي", "بيبسي"),
        ("كم آيباد باقي", "آيباد"),
        ("adde 3anna Pepsi?", "Pepsi"),
        ("fi P1001 available?", "P1001"),
        ("adde ba2e men WATER-1500?", "WATER-1500"),
    ],
)
def test_multilingual_quantity_turn_reaches_inventory_with_product_reference(
    api_client: TestClient,
    db_session: Session,
    message: str,
    reference: str,
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email=f"quantity-{uuid.uuid4()}@example.com",
    )
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"product_filter": reference, "limit": 5},
            ),
            usage_result(reply="There are 8 available units."),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)

    response = submit(
        api_client,
        user,
        business["id"],
        message,
        f"quantity-{uuid.uuid4()}",
    )

    assert response.status_code == 200, response.text
    assert CURRENT_INVENTORY_TOOL in {tool.name for tool in provider.requests[0].tools}
    assert source.resolution_references == [reference]
    assert source.calls == [CURRENT_INVENTORY_TOOL]


@pytest.mark.parametrize("status", ["ambiguous", "not_found"])
def test_unresolved_product_turn_returns_safe_answer_without_rag_or_guessing(
    api_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email=f"resolution-{status}-{uuid.uuid4()}@example.com",
    )
    source = StubSource()
    candidates = (
        (
            ProductResolutionCandidate(
                external_product_id="P1007", sku="PEPSI-330", name="Pepsi 330 ml"
            ),
            ProductResolutionCandidate(
                external_product_id="P1008",
                sku="PEPSI-1500",
                name="Pepsi 1.5 L",
            ),
        )
        if status == "ambiguous"
        else ()
    )
    source.resolution = ProductResolution(
        status=cast(Any, status),
        matched_by="partial_name" if status == "ambiguous" else None,
        candidates=candidates,
        metadata=metadata(rows=len(candidates)),
    )
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"product_filter": "Pepsi", "limit": 5},
            ),
            usage_result(reply="Use the first product and assume 99 units."),
        ]
    )
    monkeypatch.setattr(
        owner_chat,
        "create_embedding_provider",
        lambda _settings: (_ for _ in ()).throw(
            AssertionError("product resolution must not fall back to RAG")
        ),
    )
    configure_operational_chat(db_session, business["id"], source, provider)

    response = submit(
        api_client,
        user,
        business["id"],
        "How many Pepsi do we have left?",
        f"resolution-{status}",
    )

    assert response.status_code == 200, response.text
    content = response.json()["assistant_message"]["content"]
    assert "99" not in content
    assert response.json()["assistant_message"]["sources"] == []
    if status == "ambiguous":
        assert "Which one do you mean?" in content
        assert "PEPSI-330" in content and "PEPSI-1500" in content
    else:
        assert "couldn't find that product" in content
    assert source.calls == []
    assert db_session.scalar(select(func.count()).select_from(ToolCallLog)) == 1


def test_active_source_without_matching_capability_keeps_safe_unavailable_bypass(
    api_client: TestClient, db_session: Session, migration_engine: Engine
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email="unsupported-live@example.com",
        name="Unsupported Live Store",
    )
    source = StubSource()
    provider = SequenceProvider([])
    configure_operational_chat(db_session, business["id"], source, provider)

    response = submit(
        api_client,
        user,
        business["id"],
        "How many customer orders are pending today?",
        "unsupported-live-operation",
    )

    assert response.status_code == 200, response.text
    assert "live operational" in response.json()["assistant_message"]["content"].lower()
    assert provider.requests == []
    assert source.calls == []
    assert db_session.scalar(select(func.count()).select_from(ToolCallLog)) == 0
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM ai_usage_reservations")) == 0
        )


def test_unhealthy_source_keeps_safe_unavailable_bypass(
    api_client: TestClient, db_session: Session, migration_engine: Engine
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email="unhealthy-live@example.com",
        name="Unhealthy Live Store",
    )
    source = StubSource()
    source.health_error = RuntimeError("private connection detail")
    provider = SequenceProvider([])
    configure_operational_chat(db_session, business["id"], source, provider)

    response = submit(
        api_client,
        user,
        business["id"],
        "What is current inventory now?",
        "unhealthy-live-operation",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert "live operational" in body["assistant_message"]["content"].lower()
    assert "private connection detail" not in response.text
    assert provider.requests == []
    assert db_session.scalar(select(func.count()).select_from(ToolCallLog)) == 0
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM ai_usage_reservations")) == 0
        )


def test_repeated_tool_request_is_rejected_without_second_execution(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email="loop-limit@example.com",
        name="Loop Limit Store",
    )
    source = StubSource()
    tool_request = {
        "decision": "tool",
        "tool_name": CURRENT_INVENTORY_TOOL,
        "tool_arguments": {"limit": 5},
    }
    provider = SequenceProvider(
        [usage_result(**tool_request), usage_result(**tool_request)]
    )
    configure_operational_chat(db_session, business["id"], source, provider)

    response = submit(
        api_client,
        user,
        business["id"],
        "How many current products are in stock now?",
        "repeated-live-tool",
    )

    assert response.status_code == 200, response.text
    assert "live operational" in response.json()["assistant_message"]["content"].lower()
    assert len(provider.requests) == 2
    assert source.calls == [CURRENT_INVENTORY_TOOL]
    audits = db_session.scalars(
        select(ToolCallLog).order_by(ToolCallLog.created_at, ToolCallLog.id)
    ).all()
    assert [audit.status for audit in audits] == [
        ToolCallStatus.SUCCESS,
        ToolCallStatus.DENIED,
    ]
    assert audits[1].error_code == "loop_limit"


def test_loop_allows_two_tools_and_requires_final_by_third_provider_call(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email="bounded-loop@example.com",
        name="Bounded Loop Store",
    )
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"limit": 5},
            ),
            usage_result(
                decision="tool",
                tool_name=SALES_SUMMARY_TOOL,
                tool_arguments={
                    "start_date": "2026-08-20",
                    "end_date": "2026-08-23",
                },
            ),
            usage_result(
                reply=(
                    "Current stock has 8 available units; net sales were "
                    "2,000,000 LBP for 2026-08-20 through 2026-08-23 "
                    "in Asia/Beirut."
                )
            ),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)

    response = submit(
        api_client,
        user,
        business["id"],
        "Show current stock and sales this week.",
        "bounded-two-tools",
    )

    assert response.status_code == 200, response.text
    assert len(provider.requests) == 3
    assert source.calls == [CURRENT_INVENTORY_TOOL, SALES_SUMMARY_TOOL]
    assert db_session.scalar(select(func.count()).select_from(ToolCallLog)) == 2
    final_context = provider.requests[2].tool_results
    assert [item.tool_name for item in final_context] == [
        CURRENT_INVENTORY_TOOL,
        SALES_SUMMARY_TOOL,
    ]


def test_failed_loop_finalizes_reservation_and_terminal_replay_does_no_work(
    api_client: TestClient, db_session: Session, migration_engine: Engine
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email="failed-loop@example.com",
        name="Failed Loop Store",
    )
    source = StubSource()
    provider = FailingSecondProvider(
        [
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"limit": 5},
            )
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)

    first = submit(
        api_client,
        user,
        business["id"],
        "What is current inventory now?",
        "failed-live-loop",
    )
    replay = submit(
        api_client,
        user,
        business["id"],
        "What is current inventory now?",
        "failed-live-loop",
    )

    assert first.status_code == 503
    assert first.json()["error"]["code"] == "assistant_timeout"
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "owner_turn_failed"
    assert len(provider.requests) == 2
    assert source.calls == [CURRENT_INVENTORY_TOOL]
    assert db_session.scalar(select(func.count()).select_from(ToolCallLog)) == 1
    with migration_engine.connect() as connection:
        reservation = connection.execute(
            text(
                "SELECT status, reserved_tokens, total_tokens "
                "FROM ai_usage_reservations"
            )
        ).one()
    assert reservation.status == "charged"
    assert reservation.total_tokens == reservation.reserved_tokens


def test_concurrent_idempotent_operational_submissions_execute_once(
    api_client: TestClient,
    db_session: Session,
    database_engine: Engine,
    migration_engine: Engine,
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email="concurrent-live@example.com",
        name="Concurrent Live Store",
    )
    business_id = uuid.UUID(str(business["id"]))
    user_id = cast(User, user).id
    active_source(db_session, business_id)
    source = StubSource()
    profiles = cast(ConnectionProfileRegistry, StubRegistry(source))
    provider = BlockingOperationalProvider(
        [
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"limit": 5},
            ),
            usage_result(reply="The current result has 8 available units."),
        ]
    )
    settings = audit_settings()
    factory = sessionmaker(bind=database_engine, expire_on_commit=False)

    def attempt() -> object:
        with factory() as session:
            thread_user = session.get(User, user_id)
            assert thread_user is not None
            return submit_owner_message(
                session,
                thread_user,
                business_id,
                OwnerMessageRequest(
                    idempotency_key="same-live-concurrent",
                    content="What is current inventory now?",
                ),
                provider,
                settings,
                profiles,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt)
        assert provider.started.wait(timeout=5)
        second = pool.submit(attempt)
        provider.release.set()
        results = [first.result(timeout=10), second.result(timeout=10)]

    assert results[0].assistant_message.id == results[1].assistant_message.id
    assert len(provider.requests) == 2
    assert source.calls == [CURRENT_INVENTORY_TOOL]
    assert db_session.scalar(select(func.count()).select_from(ToolCallLog)) == 1
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM ai_usage_reservations")) == 1
        )
