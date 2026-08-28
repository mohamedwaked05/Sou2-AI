"""Controlled registry, executor, audit, and owner-loop coverage."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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
    UserOperationalPreference,
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
    LocationCandidate,
    LocationResolution,
    MetricCapabilityResult,
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
        self.category_references: list[str] = []
        self.last_inventory_query: Any | None = None
        self.last_restocking_query: Any | None = None
        self.error: Exception | None = None
        self.health_error: Exception | None = None
        self.categories = (
            CategoryCandidate(external_category_id="category-1", label="Pantry"),
        )
        self.locations = (
            LocationCandidate(
                external_location_id="BR-JBEIL",
                label="Jbeil Branch",
                location_type="branch",
            ),
            LocationCandidate(
                external_location_id="BR-OTHER",
                label="Other Branch",
                location_type="branch",
            ),
            LocationCandidate(
                external_location_id="WH-OTHER",
                label="Other Warehouse",
                location_type="warehouse",
            ),
        )
        self.timeout_seconds = 2
        self.resolution = ProductResolution(
            status="resolved",
            matched_by="alias",
            product=inventory_item().product,
            metadata=metadata(),
        )
        self.category_resolution = CategoryResolution(
            status="resolved",
            category=CategoryCandidate(
                external_category_id="category-1", label="Pantry"
            ),
            metadata=metadata(),
        )

    @property
    def enforced_query_timeout_seconds(self) -> int:
        return self.timeout_seconds

    def resolve_product(self, query: object) -> ProductResolution:
        self.resolution_references.append(cast(Any, query).reference)
        return self.resolution

    def resolve_category(self, query: object) -> CategoryResolution:
        self.category_references.append(cast(Any, query).reference)
        return self.category_resolution

    def list_categories(self, *, limit: int) -> tuple[CategoryCandidate, ...]:
        return self.categories[:limit]

    def resolve_location(self, query: object) -> LocationResolution:
        reference = cast(Any, query).reference.casefold()
        matches = tuple(
            candidate
            for candidate in self.locations
            if reference in candidate.label.casefold()
            or reference == candidate.external_location_id.casefold()
        )
        if not matches:
            return LocationResolution(status="not_found", metadata=metadata(rows=0))
        if len(matches) > 1:
            return LocationResolution(
                status="ambiguous",
                candidates=matches,
                metadata=metadata(rows=len(matches)),
            )
        return LocationResolution(
            status="resolved", location=matches[0], metadata=metadata()
        )

    def list_locations(self, *, limit: int) -> tuple[LocationCandidate, ...]:
        return self.locations[:limit]

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
    inventory_properties = registry[CURRENT_INVENTORY_TOOL].provider_schema()[
        "input_schema"
    ]["properties"]
    assert "location_reference" in inventory_properties
    assert "branch_external_id" not in inventory_properties
    assert "warehouse_external_id" not in inventory_properties


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


@pytest.mark.parametrize(
    "tool_name", [CURRENT_INVENTORY_TOOL, RESTOCKING_RECOMMENDATIONS_TOOL]
)
def test_category_scoped_tools_resolve_before_read_query(
    api_client: TestClient, db_session: Session, tool_name: str
) -> None:
    user, business, registry, executor = executor_setup(api_client, db_session)

    result = executor.execute(
        user=user,
        business_id=business.id,
        tool_name=tool_name,
        arguments={"category_filter": "pan", "limit": 5},
    )

    assert cast(Any, result.output).category_resolution.status == "resolved"
    read_query = (
        registry.source.last_inventory_query
        if tool_name == CURRENT_INVENTORY_TOOL
        else registry.source.last_restocking_query
    )
    assert read_query.category_filter == "Pantry"
    assert registry.source.category_references == ["pan"]
    assert registry.source.resolution_references == []


@pytest.mark.parametrize(
    "tool_name", [CURRENT_INVENTORY_TOOL, RESTOCKING_RECOMMENDATIONS_TOOL]
)
def test_unknown_category_short_circuits_category_scoped_tools(
    api_client: TestClient, db_session: Session, tool_name: str
) -> None:
    user, business, registry, executor = executor_setup(api_client, db_session)
    registry.source.category_resolution = CategoryResolution(
        status="not_found", metadata=metadata(rows=0)
    )

    result = executor.execute(
        user=user,
        business_id=business.id,
        tool_name=tool_name,
        arguments={"category_filter": "unmatched category", "limit": 5},
    )

    assert cast(Any, result.output).category_resolution.status == "not_found"
    assert registry.source.category_references == ["unmatched category"]
    assert registry.source.calls == []


def test_unknown_category_reaches_inventory_resolution_without_source_fallback(
    api_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, business = active_business(
        api_client, db_session, email=f"unknown-category-{uuid.uuid4()}@example.com"
    )
    source = StubSource()
    source.category_resolution = CategoryResolution(
        status="not_found", metadata=metadata(rows=0)
    )
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"category_filter": "Electronics", "limit": 5},
            ),
            usage_result(reply="I could not find a matching live catalogue category."),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    diagnostic_messages: list[str] = []
    monkeypatch.setattr(
        owner_chat._logger,
        "info",
        lambda message, *args: diagnostic_messages.append(message % args),
    )

    response = submit(
        api_client,
        user,
        business["id"],
        "what electronics do we have?",
        f"unknown-category-{uuid.uuid4()}",
    )

    assert response.status_code == 200, response.text
    assert (
        "matching live catalogue category"
        in response.json()["assistant_message"]["content"]
    )
    assert (
        "can't access live operational data"
        not in response.json()["assistant_message"]["content"]
    )
    assert source.category_references == ["Electronics"]
    assert source.calls == []
    assert len(provider.requests) == 2
    assert provider.requests[1].mode == "operational_synthesis"
    supplied = provider.requests[1].tool_results[0].output
    assert supplied["category_resolution"]["status"] == "not_found"
    assert provider.requests[1].validated_result_status == "not_found"
    assert any(
        "category_input_kind=query" in message for message in diagnostic_messages
    )
    assert any(
        "category_resolution=zero" in message and "tool_result=not_found" in message
        for message in diagnostic_messages
    )
    assert all(
        "electronics" not in message.casefold() for message in diagnostic_messages
    )


def test_inventory_category_semantics_normalize_a_provider_final_to_inventory(
    api_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, business = active_business(
        api_client, db_session, email=f"semantic-category-{uuid.uuid4()}@example.com"
    )
    source = StubSource()
    source.category_resolution = CategoryResolution(
        status="not_found", metadata=metadata(rows=0)
    )
    provider = SequenceProvider(
        [
            usage_result(
                reply="No lookup is needed.",
                semantic_operation="inventory_category",
                entity_kind="category",
                entity_query="unresolved future category",
            ),
            usage_result(reply="No matching category was found."),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    diagnostic_messages: list[str] = []
    monkeypatch.setattr(
        owner_chat._logger,
        "info",
        lambda message, *args: diagnostic_messages.append(message % args),
    )

    response = submit(
        api_client,
        user,
        business["id"],
        "an arbitrary category request",
        f"semantic-category-{uuid.uuid4()}",
    )

    assert response.status_code == 200, response.text
    assert source.category_references == ["unresolved future category"]
    assert source.calls == []
    assert len(provider.requests) == 2
    assert provider.requests[0].mode == "operational"
    assert provider.requests[1].mode == "operational_synthesis"
    assert any(
        "semantic_operation=inventory_category" in message
        and "entity_kind=category" in message
        and "original_action=final" in message
        and "effective_action=tool" in message
        and "consistency_outcome=normalized" in message
        and "effective_tool=current_inventory" in message
        for message in diagnostic_messages
    )
    assert all(
        "unresolved future category" not in message for message in diagnostic_messages
    )


def test_category_reference_is_resolved_and_never_trusted_as_a_source_identifier(
    api_client: TestClient, db_session: Session
) -> None:
    user, business, registry, executor = executor_setup(api_client, db_session)
    registry.source.category_resolution = CategoryResolution(
        status="not_found", metadata=metadata(rows=0)
    )

    result = executor.execute(
        user=user,
        business_id=business.id,
        tool_name=CURRENT_INVENTORY_TOOL,
        arguments={"category_filter": "category-1", "limit": 5},
    )

    assert isinstance(result.output, InventoryResult)
    assert result.output.category_resolution is not None
    assert result.output.category_resolution.status == "not_found"
    assert registry.source.category_references == ["category-1"]
    assert registry.source.last_inventory_query is None
    assert registry.source.calls == []


def test_multiple_categories_return_source_derived_clarification(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client, db_session, email=f"ambiguous-category-{uuid.uuid4()}@example.com"
    )
    source = StubSource()
    source.category_resolution = CategoryResolution(
        status="ambiguous",
        candidates=(
            CategoryCandidate(external_category_id="category-2", label="Group One"),
            CategoryCandidate(external_category_id="category-3", label="Group Two"),
        ),
        metadata=metadata(rows=2),
    )
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"category_filter": "group", "limit": 5},
            )
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)

    response = submit(
        api_client,
        user,
        business["id"],
        "show me this group",
        f"ambiguous-category-{uuid.uuid4()}",
    )

    assert response.status_code == 200, response.text
    content = response.json()["assistant_message"]["content"]
    assert "Group One" in content
    assert "Group Two" in content
    assert source.calls == []
    assert len(provider.requests) == 1


def test_bounded_category_candidates_do_not_block_unresolved_source_lookup(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client, db_session, email=f"bounded-category-{uuid.uuid4()}@example.com"
    )
    source = StubSource()
    source.categories = tuple(
        CategoryCandidate(
            external_category_id=f"category-{index}", label=f"Group {index}"
        )
        for index in range(60)
    )
    source.category_resolution = CategoryResolution(
        status="not_found", metadata=metadata(rows=0)
    )
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={
                    "category_filter": "outside candidate page",
                    "limit": 5,
                },
            ),
            usage_result(reply="No matching category was found."),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)

    response = submit(
        api_client,
        user,
        business["id"],
        "show an unavailable category",
        f"bounded-category-{uuid.uuid4()}",
    )

    assert response.status_code == 200, response.text
    assert len(provider.requests[0].category_candidates) == 50
    assert source.category_references == ["outside candidate page"]
    assert provider.requests[1].validated_result_status == "not_found"


def test_final_without_tool_keeps_active_source_fallback_truthful(
    api_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, business = active_business(
        api_client, db_session, email=f"planner-final-{uuid.uuid4()}@example.com"
    )
    source = StubSource()
    provider = SequenceProvider([usage_result(reply="No tool is needed.")])
    configure_operational_chat(db_session, business["id"], source, provider)
    diagnostic_messages: list[str] = []
    monkeypatch.setattr(
        owner_chat._logger,
        "info",
        lambda message, *args: diagnostic_messages.append(message % args),
    )

    response = submit(
        api_client,
        user,
        business["id"],
        "Please provide a live operational answer.",
        f"planner-final-{uuid.uuid4()}",
    )

    assert response.status_code == 200, response.text
    content = response.json()["assistant_message"]["content"]
    assert "live operational source is available" in content
    assert "can't access live operational data" not in content
    assert source.calls == []
    assert any(
        "fallback_reason=provider_final_without_tool_active_source" in message
        for message in diagnostic_messages
    )


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


def test_profit_metric_returns_missing_capability_and_revenue_alternative(
    api_client: TestClient, db_session: Session
) -> None:
    user, business, _registry, executor = executor_setup(api_client, db_session)

    result = executor.execute(
        user=user,
        business_id=business.id,
        tool_name=SALES_SUMMARY_TOOL,
        arguments={
            "start_date": "2026-08-20",
            "end_date": "2026-08-23",
            "metric": "gross_profit",
        },
    )

    assert isinstance(result.output, MetricCapabilityResult)
    assert result.output.status == "unsupported"
    assert result.output.requested_metric == "gross_profit"
    assert result.output.missing_inputs == ("cost_cogs",)
    assert result.output.supported_metrics == ("revenue", "sales_count")
    assert result.output.period.start_date.isoformat() == "2026-08-20"


def test_owner_receives_unsupported_profit_facts_and_preserves_franco_style(
    api_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, business = active_business(
        api_client, db_session, email=f"profit-capability-{uuid.uuid4()}@example.com"
    )
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                tool_name=SALES_SUMMARY_TOOL,
                tool_arguments={
                    "start_date": "2026-08-20",
                    "end_date": "2026-08-23",
                    "metric": "gross_profit",
                },
            ),
            usage_result(
                reply=(
                    "Ma fina n7seb l profit accurately la2an cost/COGS mish "
                    "connected. Fina n3tik revenue iza بدك."
                )
            ),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)
    diagnostic_messages: list[str] = []
    monkeypatch.setattr(
        owner_chat._logger,
        "info",
        lambda message, *args: diagnostic_messages.append(message % args),
    )

    response = submit(
        api_client,
        user,
        business["id"],
        "badi a3ref adde 3melna rebe7 e5er fatra",
        f"profit-capability-{uuid.uuid4()}",
    )

    assert response.status_code == 200, response.text
    assert "COGS" in response.json()["assistant_message"]["content"]
    assert "unavailable" not in response.json()["assistant_message"]["content"]
    assert SALES_SUMMARY_TOOL in {tool.name for tool in provider.requests[0].tools}
    assert provider.requests[1].mode == "operational_synthesis"
    assert provider.requests[1].tools == ()
    supplied = provider.requests[1].tool_results[0].output
    assert supplied["requested_metric"] == "gross_profit"
    assert supplied["supported_metrics"] == ["revenue", "sales_count"]
    assert "minimarket" not in str(supplied)
    assert any("metric=gross_profit" in message for message in diagnostic_messages)
    assert any(
        "capability_outcome=unsupported" in message for message in diagnostic_messages
    )
    assert any(
        "owner_chat_operational_synthesis outcome=final schema=response_only" in message
        for message in diagnostic_messages
    )
    assert all("rebe7" not in message for message in diagnostic_messages)
    assert all("Ma fina" not in message for message in diagnostic_messages)


def test_location_preference_is_saved_without_an_inventory_read_and_applied_later(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client, db_session, email=f"location-preference-{uuid.uuid4()}@example.com"
    )
    source = StubSource()
    preference_provider = SequenceProvider(
        [
            usage_result(
                decision="set_preference",
                preference_key="default_inventory_location",
                location_reference="Jbeil",
            ),
            usage_result(
                reply="I will use that location for future inventory questions."
            ),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, preference_provider)

    preference = submit(
        api_client,
        user,
        business["id"],
        "Please use this branch for my later inventory questions.",
        f"location-preference-{uuid.uuid4()}",
    )

    assert preference.status_code == 200, preference.text
    assert "BR-JBEIL" not in str(preference_provider.requests[0])
    assert preference_provider.requests[1].mode == "operational_synthesis"
    assert preference_provider.requests[1].tools == ()
    assert source.calls == []
    saved = db_session.scalar(select(UserOperationalPreference))
    assert saved is not None
    assert saved.location_external_id == "BR-JBEIL"

    inventory_provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"product_filter": "generic-item", "limit": 5},
            ),
            usage_result(reply="The validated inventory result is ready."),
        ]
    )
    app.dependency_overrides[get_owner_chat_provider] = lambda: inventory_provider
    inventory = submit(
        api_client,
        user,
        business["id"],
        "How many generic items do we have?",
        f"location-preference-inventory-{uuid.uuid4()}",
    )

    assert inventory.status_code == 200, inventory.text
    assert source.last_inventory_query.branch_external_id == "BR-JBEIL"


def test_explicit_inventory_location_overrides_saved_preference(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client, db_session, email=f"location-override-{uuid.uuid4()}@example.com"
    )
    source = StubSource()
    configure_operational_chat(db_session, business["id"], source, SequenceProvider([]))
    active = db_session.scalar(select(OperationalDataSourceConfig))
    assert active is not None
    db_session.add(
        UserOperationalPreference(
            user_id=user.id,
            business_id=uuid.UUID(str(business["id"])),
            source_id=active.id,
            preference_key="default_inventory_location",
            location_type="branch",
            location_external_id="BR-JBEIL",
        )
    )
    db_session.commit()
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={
                    "product_filter": "generic-item",
                    "location_reference": "Other Branch",
                    "limit": 5,
                },
            ),
            usage_result(reply="The validated inventory result is ready."),
        ]
    )
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider

    response = submit(
        api_client,
        user,
        business["id"],
        "How many generic items are at the other location?",
        f"location-override-{uuid.uuid4()}",
    )

    assert response.status_code == 200, response.text
    assert source.last_inventory_query.branch_external_id == "BR-OTHER"
    assert (
        db_session.scalar(select(UserOperationalPreference)).location_external_id
        == "BR-JBEIL"
    )


def test_inventory_planning_ignores_history_and_applies_saved_location(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email=f"current-turn-location-{uuid.uuid4()}@example.com",
    )
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(
                decision="set_preference",
                preference_key="default_inventory_location",
                location_reference="Jbeil Branch",
            ),
            usage_result(reply="The default location was saved."),
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"product_filter": "generic-item", "limit": 5},
            ),
            usage_result(reply="The validated inventory result is ready."),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)

    preference = submit(
        api_client,
        user,
        business["id"],
        "Use Jbeil Branch for future inventory questions.",
        f"history-location-preference-{uuid.uuid4()}",
    )
    inventory = submit(
        api_client,
        user,
        business["id"],
        "How many generic items do we have?",
        f"history-location-inventory-{uuid.uuid4()}",
    )

    assert preference.status_code == 200, preference.text
    assert inventory.status_code == 200, inventory.text
    planning_request = provider.requests[2]
    assert planning_request.mode == "operational"
    assert len(planning_request.messages) == 1
    assert planning_request.messages[0].content == "How many generic items do we have?"
    assert source.last_inventory_query.branch_external_id == "BR-JBEIL"
    saved = db_session.scalar(select(UserOperationalPreference))
    assert saved is not None
    assert saved.location_external_id == "BR-JBEIL"


def test_location_preference_clear_is_idempotent_and_never_reads_inventory(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client, db_session, email=f"location-clear-{uuid.uuid4()}@example.com"
    )
    source = StubSource()
    configure_operational_chat(db_session, business["id"], source, SequenceProvider([]))
    active = db_session.scalar(
        select(OperationalDataSourceConfig).where(
            OperationalDataSourceConfig.business_id == uuid.UUID(str(business["id"]))
        )
    )
    assert active is not None
    db_session.add(
        UserOperationalPreference(
            user_id=user.id,
            business_id=uuid.UUID(str(business["id"])),
            source_id=active.id,
            preference_key="default_inventory_location",
            location_type="branch",
            location_external_id="BR-JBEIL",
        )
    )
    db_session.commit()
    provider = SequenceProvider(
        [
            usage_result(
                decision="clear_preference",
                preference_key="default_inventory_location",
            ),
            usage_result(reply="The default inventory location is cleared."),
        ]
    )
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider

    response = submit(
        api_client,
        user,
        business["id"],
        "Do not use my inventory location default anymore.",
        f"location-clear-{uuid.uuid4()}",
    )

    assert response.status_code == 200, response.text
    assert provider.requests[1].mode == "operational_synthesis"
    assert source.calls == []
    assert (
        db_session.scalar(
            select(UserOperationalPreference).where(
                UserOperationalPreference.user_id == user.id,
                UserOperationalPreference.business_id == uuid.UUID(str(business["id"])),
            )
        )
        is None
    )


def test_ambiguous_location_preference_is_not_saved_or_exposes_source_ids(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client, db_session, email=f"location-ambiguous-{uuid.uuid4()}@example.com"
    )
    source = StubSource()
    source.locations = (
        LocationCandidate(
            external_location_id="BR-ONE", label="North Branch", location_type="branch"
        ),
        LocationCandidate(
            external_location_id="BR-TWO", label="South Branch", location_type="branch"
        ),
    )
    provider = SequenceProvider(
        [
            usage_result(
                decision="set_preference",
                preference_key="default_inventory_location",
                location_reference="Branch",
            ),
            usage_result(reply="Which location do you mean?"),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)

    response = submit(
        api_client,
        user,
        business["id"],
        "Use this location for future inventory questions.",
        f"location-ambiguous-{uuid.uuid4()}",
    )

    assert response.status_code == 200, response.text
    supplied = provider.requests[1].tool_results[0].output
    assert supplied["action"] == "not_saved"
    assert [
        candidate["label"] for candidate in supplied["resolution"]["candidates"]
    ] == [
        "North Branch",
        "South Branch",
    ]
    assert "external_location_id" not in str(supplied)
    assert db_session.scalar(select(UserOperationalPreference)) is None
    assert source.calls == []


def test_stale_location_preference_is_removed_without_an_inventory_query(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client, db_session, email=f"location-stale-{uuid.uuid4()}@example.com"
    )
    source = StubSource()
    configure_operational_chat(db_session, business["id"], source, SequenceProvider([]))
    active = db_session.scalar(
        select(OperationalDataSourceConfig).where(
            OperationalDataSourceConfig.business_id == uuid.UUID(str(business["id"]))
        )
    )
    assert active is not None
    db_session.add(
        UserOperationalPreference(
            user_id=user.id,
            business_id=uuid.UUID(str(business["id"])),
            source_id=active.id,
            preference_key="default_inventory_location",
            location_type="branch",
            location_external_id="BR-REMOVED",
        )
    )
    db_session.commit()
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={"product_filter": "generic-item", "limit": 5},
            ),
            usage_result(reply="Your saved inventory location is no longer available."),
        ]
    )
    app.dependency_overrides[get_owner_chat_provider] = lambda: provider

    response = submit(
        api_client,
        user,
        business["id"],
        "How many generic items do we have?",
        f"location-stale-{uuid.uuid4()}",
    )

    assert response.status_code == 200, response.text
    assert source.calls == []
    assert provider.requests[1].mode == "operational_synthesis"
    assert provider.requests[1].tool_results[0].output["action"] == "invalidated"
    assert db_session.scalar(select(UserOperationalPreference)) is None


def test_unsupported_profit_uses_capability_fallback_when_synthesis_is_invalid(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client, db_session, email=f"profit-fallback-{uuid.uuid4()}@example.com"
    )
    source = StubSource()
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                tool_name=SALES_SUMMARY_TOOL,
                tool_arguments={
                    "start_date": "2026-08-20",
                    "end_date": "2026-08-23",
                    "metric": "gross_profit",
                },
            ),
            usage_result(
                decision="tool",
                tool_name=SALES_SUMMARY_TOOL,
                tool_arguments={"metric": "gross_profit"},
            ),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)

    response = submit(
        api_client,
        user,
        business["id"],
        "badi a3ref adde 3melna rebe7 e5er fatra",
        f"profit-fallback-{uuid.uuid4()}",
    )

    assert response.status_code == 200, response.text
    content = response.json()["assistant_message"]["content"]
    assert "cost/COGS" in content
    assert "revenue" in content
    assert "sales count" in content
    assert "live operational data" not in content.lower()
    assert len(provider.requests) == 2
    assert provider.requests[1].mode == "operational_synthesis"
    assert source.calls == []


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
        result = self.results[len(self.requests) - 1]
        if (
            request.mode == "operational_synthesis"
            and result.validated_result_status is None
        ):
            return replace(
                result, validated_result_status=request.validated_result_status
            )
        return result


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
    values.setdefault("semantic_operation", "unsupported")
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
                tool_arguments={"location_reference": "Jbeil Branch", "limit": 5},
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


def test_product_follow_up_uses_bounded_pending_candidates_without_history(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email=f"pending-product-{uuid.uuid4()}@example.com",
    )
    source = StubSource()
    ambiguous = ProductResolution(
        status="ambiguous",
        matched_by="partial_name",
        candidates=(
            ProductResolutionCandidate(
                external_product_id="fixture-product-a",
                sku="fixture-a",
                name="Fixture Product A",
            ),
            ProductResolutionCandidate(
                external_product_id="fixture-product-b",
                sku="fixture-b",
                name="Fixture Product B",
            ),
        ),
        metadata=metadata(rows=2),
    )
    resolved = source.resolution

    def resolve_by_reference(query: object) -> ProductResolution:
        reference = cast(Any, query).reference
        source.resolution_references.append(reference)
        return ambiguous if reference == "initial ambiguous request" else resolved

    source.resolve_product = resolve_by_reference  # type: ignore[method-assign]
    provider = SequenceProvider(
        [
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={
                    "product_filter": "initial ambiguous request",
                    "limit": 5,
                },
            ),
            usage_result(
                decision="tool",
                tool_name=CURRENT_INVENTORY_TOOL,
                tool_arguments={
                    "product_filter": "selected fixture variant",
                    "limit": 5,
                },
            ),
            usage_result(reply="The validated inventory result is ready."),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)

    initial = submit(
        api_client,
        user,
        business["id"],
        "initial ambiguous request",
        f"pending-product-initial-{uuid.uuid4()}",
    )
    follow_up = submit(
        api_client,
        user,
        business["id"],
        "selected fixture variant",
        f"pending-product-follow-up-{uuid.uuid4()}",
    )

    assert initial.status_code == 200, initial.text
    assert follow_up.status_code == 200, follow_up.text
    follow_up_request = provider.requests[1]
    assert len(follow_up_request.messages) == 1
    assert follow_up_request.messages[0].content == "selected fixture variant"
    assert [
        candidate.label for candidate in follow_up_request.pending_product_candidates
    ] == [
        "Fixture Product A",
        "Fixture Product B",
    ]
    assert source.last_inventory_query is not None


def test_active_source_without_matching_capability_uses_provider_unavailable(
    api_client: TestClient, db_session: Session, migration_engine: Engine
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email="unsupported-live@example.com",
        name="Unsupported Live Store",
    )
    source = StubSource()
    provider = SequenceProvider(
        [usage_result(decision="unavailable", reply="No supported tool is available.")]
    )
    configure_operational_chat(db_session, business["id"], source, provider)

    response = submit(
        api_client,
        user,
        business["id"],
        "How many customer orders are pending today?",
        "unsupported-live-operation",
    )

    assert response.status_code == 200, response.text
    assert "supported tool" in response.json()["assistant_message"]["content"].lower()
    assert len(provider.requests) == 1
    assert source.calls == []
    assert db_session.scalar(select(func.count()).select_from(ToolCallLog)) == 0
    with migration_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM ai_usage_reservations")) == 1
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


def test_response_only_synthesis_rejects_a_tool_request_without_replanning(
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
    assert (
        "validated inventory" in response.json()["assistant_message"]["content"].lower()
    )
    assert len(provider.requests) == 2
    assert provider.requests[1].mode == "operational_synthesis"
    assert provider.requests[1].tools == ()
    assert source.calls == [CURRENT_INVENTORY_TOOL]
    audits = db_session.scalars(
        select(ToolCallLog).order_by(ToolCallLog.created_at, ToolCallLog.id)
    ).all()
    assert [audit.status for audit in audits] == [ToolCallStatus.SUCCESS]


def test_tool_result_uses_response_only_synthesis_without_a_second_plan(
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
    assert len(provider.requests) == 2
    assert source.calls == [CURRENT_INVENTORY_TOOL]
    assert db_session.scalar(select(func.count()).select_from(ToolCallLog)) == 1
    assert provider.requests[1].mode == "operational_synthesis"
    assert provider.requests[1].tools == ()
    assert [item.tool_name for item in provider.requests[1].tool_results] == [
        CURRENT_INVENTORY_TOOL
    ]


def test_inventory_synthesis_cannot_label_positive_validated_stock_as_empty(
    api_client: TestClient, db_session: Session
) -> None:
    user, business = active_business(
        api_client,
        db_session,
        email="synthesis-inventory-state@example.com",
        name="Synthesis Inventory State Store",
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
                reply="No matching stock is available.",
                validated_result_status="empty",
            ),
        ]
    )
    configure_operational_chat(db_session, business["id"], source, provider)

    response = submit(
        api_client,
        user,
        business["id"],
        "Inventory quantity request.",
        "synthesis-inventory-state",
    )

    assert response.status_code == 200, response.text
    assert provider.requests[1].mode == "operational_synthesis"
    assert provider.requests[1].validated_result_status == "data"
    assert (
        "validated inventory" in response.json()["assistant_message"]["content"].lower()
    )
    assert source.calls == [CURRENT_INVENTORY_TOOL]


def test_synthesis_failure_uses_safe_fallback_and_replay_does_no_work(
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

    assert first.status_code == 200
    assert "validated inventory" in first.json()["assistant_message"]["content"].lower()
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
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
    assert reservation.status == "completed"
    assert reservation.total_tokens < reservation.reserved_tokens


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
