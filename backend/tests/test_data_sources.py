"""Tenant-scoped operational data source management and security tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest
from app.core.config import get_settings
from app.core.security import create_access_token, hash_password, utc_now
from app.database.models import (
    Business,
    BusinessMembership,
    MembershipPermission,
    OperationalDataSourceConfig,
    User,
)
from app.integrations.profiles import (
    FAKE_STORE_MAPPING,
    FAKE_STORE_PROFILE,
    ConnectionProfile,
    ConnectionProfileRegistry,
    MappingProfileError,
    OperationalMappingProfile,
    get_connection_profile_registry,
)
from app.main import app
from app.schemas.operational import IntegrationHealth
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

SAFE_BODY = {
    "display_name": "Hamra Demo Store",
    "connection_profile_key": "fake_store_postgresql",
    "mapping_profile_key": "fake_store_minimarket",
    "mapping_profile_version": 1,
}
CAPABILITIES = [
    "products",
    "inventory",
    "sales_summaries",
    "best_sellers",
    "restocking_recommendations",
]


class StubOperationalSource:
    def __init__(self) -> None:
        self.health = healthy_result()
        self.error: Exception | None = None

    def check_health(self) -> IntegrationHealth:
        if self.error is not None:
            raise self.error
        return self.health


class StubRegistry:
    def __init__(
        self,
        *,
        mapping: OperationalMappingProfile = FAKE_STORE_MAPPING,
        profile: ConnectionProfile = FAKE_STORE_PROFILE,
    ) -> None:
        self.mapping = mapping
        self.profile = profile
        self.source = StubOperationalSource()

    def available_profiles(self) -> tuple[ConnectionProfile, ...]:
        return (self.profile,)

    def get_profile(self, key: str) -> ConnectionProfile | None:
        return self.profile if key == self.profile.key else None

    def get_mapping(self, key: str, version: int) -> OperationalMappingProfile | None:
        if key == self.mapping.key and version == self.mapping.version:
            return self.mapping
        return None

    def resolve(self, key: str) -> StubOperationalSource:
        if key != self.profile.key:
            raise MappingProfileError("Operational source is unavailable.")
        return self.source


def healthy_result() -> IntegrationHealth:
    checked_at = datetime(2026, 8, 24, 10, 30, tzinfo=UTC)
    return IntegrationHealth(
        status="healthy",
        checked_at=checked_at,
        source_timezone="Asia/Beirut",
        currency="LBP",
        data_timestamp=checked_at,
    )


def create_owner_business(
    session: Session, email: str, business_name: str
) -> tuple[User, Business]:
    user = User(
        email=email,
        first_name="Store",
        last_name="Owner",
        password_hash=hash_password("Strong1!Pass"),
        email_verified_at=utc_now(),
    )
    business = Business(owner=user, name=business_name)
    membership = BusinessMembership(
        user=user,
        business=business,
        permission=MembershipPermission.FULL_ACCESS,
    )
    session.add_all([user, business, membership])
    session.commit()
    return user, business


def headers(user: User) -> dict[str, str]:
    token, _ = create_access_token(user.id, get_settings())
    return {"Authorization": f"Bearer {token}"}


def configure_registry(registry: StubRegistry) -> None:
    app.dependency_overrides[get_connection_profile_registry] = lambda: cast(
        ConnectionProfileRegistry, registry
    )


def create_source(
    client: TestClient, user: User, business: Business, **overrides: object
) -> dict[str, object]:
    body = {**SAFE_BODY, **overrides}
    response = client.post(
        f"/api/v1/businesses/{business.id}/data-sources",
        headers=headers(user),
        json=body,
    )
    assert response.status_code == 201, response.text
    return response.json()


def source_action(
    client: TestClient,
    user: User,
    business: Business,
    source_id: object,
    action: str,
):
    return client.post(
        f"/api/v1/businesses/{business.id}/data-sources/{source_id}/{action}",
        headers=headers(user),
    )


def test_create_allowlisted_source_returns_only_safe_metadata(
    api_client: TestClient, db_session: Session
) -> None:
    registry = StubRegistry()
    configure_registry(registry)
    user, business = create_owner_business(
        db_session, "allowed-source@example.com", "Allowed Store"
    )

    profiles = api_client.get(
        f"/api/v1/businesses/{business.id}/data-sources/available-profiles",
        headers=headers(user),
    )
    created = create_source(api_client, user, business)

    assert profiles.status_code == 200
    assert profiles.json()[0]["key"] == "fake_store_postgresql"
    assert created["status"] == "CONFIGURED"
    assert created["capabilities"] == CAPABILITIES
    assert created["mapping"]["completed_sale_statuses"] == [
        "COMPLETED",
        "RETURNED",
    ]
    assert created["mapping"]["excluded_sale_statuses"] == [
        "PENDING",
        "CANCELLED",
    ]
    serialized = str(created).lower()
    for forbidden in (
        "password",
        "database_url",
        "postgresql://",
        "select ",
        "catalog_items",
        "stock_levels",
    ):
        assert forbidden not in serialized

    stored = db_session.scalar(select(OperationalDataSourceConfig))
    assert stored is not None
    assert stored.business_id == business.id
    assert stored.connection_profile_key == "fake_store_postgresql"
    assert not hasattr(stored, "password")
    assert not hasattr(stored, "products")


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        (
            {"connection_profile_key": "customer_database"},
            "unsupported_connection_profile",
        ),
        (
            {"mapping_profile_key": "unknown_mapping"},
            "unsupported_mapping_profile",
        ),
        (
            {"mapping_profile_version": 2},
            "unsupported_mapping_profile",
        ),
    ],
)
def test_create_rejects_unsupported_profiles(
    api_client: TestClient,
    db_session: Session,
    overrides: dict[str, object],
    expected_code: str,
) -> None:
    configure_registry(StubRegistry())
    user, business = create_owner_business(
        db_session,
        f"{expected_code}-{len(str(overrides))}@example.com",
        "Rejected Store",
    )

    response = api_client.post(
        f"/api/v1/businesses/{business.id}/data-sources",
        headers=headers(user),
        json={**SAFE_BODY, **overrides},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == expected_code


@pytest.mark.parametrize("field", ["database_url", "password", "host", "port", "sql"])
def test_create_rejects_raw_connection_details_without_echoing_values(
    api_client: TestClient, db_session: Session, field: str
) -> None:
    configure_registry(StubRegistry())
    user, business = create_owner_business(
        db_session, f"raw-{field}@example.com", f"Raw {field} Store"
    )
    secret = "postgresql://admin:do-not-leak@private.example/store"

    response = api_client.post(
        f"/api/v1/businesses/{business.id}/data-sources",
        headers=headers(user),
        json={**SAFE_BODY, field: secret},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert secret not in response.text


@pytest.mark.parametrize(
    "mapping",
    [
        replace(FAKE_STORE_MAPPING, completed_sale_statuses=()),
        replace(
            FAKE_STORE_MAPPING,
            excluded_sale_statuses=("PENDING", "COMPLETED"),
        ),
        replace(FAKE_STORE_MAPPING, active_reservation_statuses=()),
        replace(FAKE_STORE_MAPPING, required_capabilities=("products",)),
    ],
)
def test_mapping_profile_definition_rejects_incomplete_semantics(
    mapping: OperationalMappingProfile,
) -> None:
    with pytest.raises(MappingProfileError):
        mapping.validate_definition()


def test_failed_validation_cannot_activate_and_never_leaks_exception(
    api_client: TestClient, db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    registry = StubRegistry()
    registry.source.error = RuntimeError(
        "postgresql://readonly:highly-secret@fake-store-postgres/store"
    )
    configure_registry(registry)
    user, business = create_owner_business(
        db_session, "failed-validation@example.com", "Failure Store"
    )
    source = create_source(api_client, user, business)

    validated = source_action(api_client, user, business, source["id"], "validate")
    activated = source_action(api_client, user, business, source["id"], "activate")

    assert validated.status_code == 200
    assert validated.json()["status"] == "UNHEALTHY"
    assert validated.json()["failure_code"] == "operational_source_unavailable"
    assert activated.status_code == 409
    combined_output = validated.text + activated.text + caplog.text
    assert "highly-secret" not in combined_output
    assert "postgresql://" not in combined_output


def test_validation_activation_and_disable_are_safe_and_idempotent(
    api_client: TestClient, db_session: Session
) -> None:
    configure_registry(StubRegistry())
    user, business = create_owner_business(
        db_session, "lifecycle@example.com", "Lifecycle Store"
    )
    source = create_source(api_client, user, business)

    validated = source_action(api_client, user, business, source["id"], "validate")
    active_once = source_action(api_client, user, business, source["id"], "activate")
    active_twice = source_action(api_client, user, business, source["id"], "activate")
    disabled_once = source_action(api_client, user, business, source["id"], "disable")
    disabled_twice = source_action(api_client, user, business, source["id"], "disable")

    assert validated.json()["status"] == "VALIDATED"
    assert validated.json()["last_validated_at"] is not None
    assert active_once.json()["status"] == "ACTIVE"
    assert active_twice.json()["status"] == "ACTIVE"
    assert disabled_once.json()["status"] == "DISABLED"
    assert disabled_twice.json()["status"] == "DISABLED"


def test_only_one_active_source_type_is_allowed(
    api_client: TestClient, db_session: Session
) -> None:
    configure_registry(StubRegistry())
    user, business = create_owner_business(
        db_session, "unique-active@example.com", "Unique Active Store"
    )
    first = create_source(api_client, user, business, display_name="First source")
    second = create_source(api_client, user, business, display_name="Second source")
    for source in (first, second):
        result = source_action(api_client, user, business, source["id"], "validate")
        assert result.status_code == 200

    assert (
        source_action(api_client, user, business, first["id"], "activate").status_code
        == 200
    )
    conflicting = source_action(api_client, user, business, second["id"], "activate")

    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "active_data_source_conflict"


def test_health_check_updates_success_and_failure_states(
    api_client: TestClient, db_session: Session
) -> None:
    registry = StubRegistry()
    configure_registry(registry)
    user, business = create_owner_business(
        db_session, "health-transition@example.com", "Health Store"
    )
    source = create_source(api_client, user, business)
    source_action(api_client, user, business, source["id"], "validate")
    source_action(api_client, user, business, source["id"], "activate")

    registry.source.health = IntegrationHealth(
        status="unavailable",
        checked_at=datetime(2026, 8, 24, 11, 0, tzinfo=UTC),
        error_code="operational_source_unavailable",
    )
    failed = source_action(api_client, user, business, source["id"], "health")
    assert failed.json()["status"] == "UNHEALTHY"
    assert failed.json()["failure_code"] == "operational_source_unavailable"

    registry.source.health = healthy_result()
    recovered = source_action(api_client, user, business, source["id"], "health")
    assert recovered.json()["status"] == "VALIDATED"
    assert recovered.json()["failure_code"] is None
    assert recovered.json()["last_successful_health_check_at"] is not None


def test_cross_tenant_access_is_denied_for_every_operation(
    api_client: TestClient, db_session: Session
) -> None:
    configure_registry(StubRegistry())
    owner, owned_business = create_owner_business(
        db_session, "tenant-owner@example.com", "Tenant Owner Store"
    )
    foreign, foreign_business = create_owner_business(
        db_session, "tenant-foreign@example.com", "Tenant Foreign Store"
    )
    source = create_source(api_client, foreign, foreign_business)
    base = f"/api/v1/businesses/{foreign_business.id}/data-sources"

    assert api_client.get(base, headers=headers(owner)).status_code == 404
    assert (
        api_client.get(f"{base}/available-profiles", headers=headers(owner)).status_code
        == 404
    )
    assert (
        api_client.post(base, headers=headers(owner), json=SAFE_BODY).status_code == 404
    )
    assert (
        api_client.get(f"{base}/{source['id']}", headers=headers(owner)).status_code
        == 404
    )
    for action in ("validate", "activate", "health", "disable"):
        assert (
            api_client.post(
                f"{base}/{source['id']}/{action}", headers=headers(owner)
            ).status_code
            == 404
        )

    own_listing = api_client.get(
        f"/api/v1/businesses/{owned_business.id}/data-sources",
        headers=headers(owner),
    )
    assert own_listing.status_code == 200
    assert own_listing.json() == []


def test_runtime_grants_and_database_trigger_protect_scope(
    db_session: Session,
    database_engine: Engine,
    migration_engine: Engine,
) -> None:
    _, business = create_owner_business(
        db_session, "runtime-grants@example.com", "Runtime Grants Store"
    )
    _, other_business = create_owner_business(
        db_session, "runtime-other@example.com", "Runtime Other Store"
    )
    source = OperationalDataSourceConfig(
        business_id=business.id,
        display_name="Grant Test Source",
        adapter_type="postgresql_readonly",
        connection_profile_key="fake_store_postgresql",
        mapping_profile_key="fake_store_minimarket",
        mapping_profile_version=1,
    )
    db_session.add(source)
    db_session.commit()

    with database_engine.connect() as connection:
        grants = connection.execute(
            text(
                "SELECT "
                "has_table_privilege(current_user, "
                "'operational_data_sources', 'SELECT'), "
                "has_table_privilege(current_user, "
                "'operational_data_sources', 'INSERT'), "
                "has_table_privilege(current_user, "
                "'operational_data_sources', 'DELETE'), "
                "has_column_privilege(current_user, 'operational_data_sources', "
                "'status', 'UPDATE'), "
                "has_column_privilege(current_user, 'operational_data_sources', "
                "'business_id', 'UPDATE')"
            )
        ).one()
    assert grants == (True, True, False, True, False)

    with database_engine.connect() as connection, pytest.raises(DBAPIError):
        connection.execute(
            text("DELETE FROM operational_data_sources WHERE id = :source_id"),
            {"source_id": source.id},
        )

    with migration_engine.connect() as connection, pytest.raises(DBAPIError):
        connection.execute(
            text(
                "UPDATE operational_data_sources SET business_id = :business_id "
                "WHERE id = :source_id"
            ),
            {"business_id": other_business.id, "source_id": source.id},
        )


def test_platform_schema_contains_no_copied_operational_tables(
    migration_engine: Engine,
) -> None:
    with migration_engine.connect() as connection:
        tables = set(
            connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            ).scalars()
        )
        columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'operational_data_sources'"
                )
            ).scalars()
        )

    assert not tables.intersection(
        {"catalog_items", "stock_levels", "sale_headers", "sale_lines", "refunds"}
    )
    assert not columns.intersection(
        {"database_url", "password", "host", "port", "sql", "products", "sales"}
    )
