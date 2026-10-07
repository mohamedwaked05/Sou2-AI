"""Allowlisted deployment connection profiles and semantic source mappings."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock
from typing import Literal, Protocol, runtime_checkable

from pydantic import TypeAdapter, ValidationError

from app.core.config import Settings, get_settings
from app.integrations.discovery import EngineConnector, SourceConnection
from app.integrations.operational import OperationalDataSource
from app.integrations.postgresql import create_fake_store_adapter
from app.schemas.operational import IntegrationHealth

FAKE_STORE_CONNECTION_PROFILE = "fake_store_postgresql"
FAKE_STORE_MAPPING_PROFILE = "fake_store_minimarket"
FAKE_STORE_MAPPING_VERSION = 1
POSTGRESQL_READONLY_ADAPTER = "postgresql_readonly"
CanonicalLocationType = Literal["branch", "warehouse"]

OPERATIONAL_CAPABILITIES = (
    "products",
    "inventory",
    "sales_summaries",
    "best_sellers",
    "restocking_recommendations",
)


class MappingProfileError(ValueError):
    """Safe signal that a semantic mapping is incomplete or mismatched."""


@dataclass(frozen=True)
class OperationalMappingProfile:
    key: str
    version: int
    display_name: str
    completed_sale_statuses: tuple[str, ...]
    excluded_sale_statuses: tuple[str, ...]
    return_treatment: str
    active_reservation_statuses: tuple[str, ...]
    reservation_treatment: str
    branch_meaning: str
    warehouse_meaning: str
    quantity_interpretation: str
    revenue_interpretation: str
    currency: str
    source_timezone: str
    required_capabilities: tuple[str, ...]
    source_location_types: Mapping[CanonicalLocationType, tuple[str, ...]]
    supported_metrics: tuple[str, ...] = ("revenue", "sales_count")

    def validate_definition(self) -> None:
        required_text = (
            self.key,
            self.display_name,
            self.return_treatment,
            self.reservation_treatment,
            self.branch_meaning,
            self.warehouse_meaning,
            self.quantity_interpretation,
            self.revenue_interpretation,
            self.currency,
            self.source_timezone,
        )
        if self.version < 1 or any(not value.strip() for value in required_text):
            raise MappingProfileError("Operational mapping profile is incomplete.")
        if not self.completed_sale_statuses or not self.excluded_sale_statuses:
            raise MappingProfileError(
                "Operational sale status semantics are incomplete."
            )
        if set(self.completed_sale_statuses) & set(self.excluded_sale_statuses):
            raise MappingProfileError("Operational sale status semantics conflict.")
        if not self.active_reservation_statuses:
            raise MappingProfileError(
                "Operational reservation semantics are incomplete."
            )
        if not self.supported_metrics or not set(self.supported_metrics) <= {
            "revenue",
            "sales_count",
            "gross_profit",
            "net_profit",
            "inventory_value",
        }:
            raise MappingProfileError("Operational metric semantics are incomplete.")
        supported = set(self.supported_metrics)
        if "gross_profit" in supported and "revenue" not in supported:
            raise MappingProfileError("Gross profit requires revenue support.")
        if "net_profit" in supported and "gross_profit" not in supported:
            raise MappingProfileError("Net profit requires gross profit support.")
        if set(self.required_capabilities) != set(OPERATIONAL_CAPABILITIES):
            raise MappingProfileError("Operational capability mapping is incomplete.")
        if set(self.source_location_types) != {"branch", "warehouse"} or any(
            not values or any(not value.strip() for value in values)
            for values in self.source_location_types.values()
        ):
            raise MappingProfileError(
                "Operational location type mapping is incomplete."
            )
        mapped_values = tuple(
            value for values in self.source_location_types.values() for value in values
        )
        if len({value.casefold() for value in mapped_values}) != len(mapped_values):
            raise MappingProfileError("Operational location type mapping conflicts.")

    def validate_health(self, health: IntegrationHealth) -> None:
        self.validate_definition()
        if health.status != "healthy":
            raise MappingProfileError("Operational source is unavailable.")
        if health.currency != self.currency:
            raise MappingProfileError("Operational source currency does not match.")
        if health.source_timezone != self.source_timezone:
            raise MappingProfileError("Operational source timezone does not match.")


@dataclass(frozen=True)
class ConnectionProfile:
    key: str
    display_name: str
    description: str
    adapter_type: str
    mapping_profile_key: str
    mapping_profile_version: int
    business_ids: tuple[uuid.UUID, ...] = ()


FAKE_STORE_MAPPING = OperationalMappingProfile(
    key=FAKE_STORE_MAPPING_PROFILE,
    version=FAKE_STORE_MAPPING_VERSION,
    display_name="Lebanese Minimarket POS Mapping",
    completed_sale_statuses=("COMPLETED", "RETURNED"),
    excluded_sale_statuses=("PENDING", "CANCELLED"),
    return_treatment=(
        "Finalized sales remain in gross totals; completed refunds subtract "
        "quantity and revenue at the refund timestamp."
    ),
    active_reservation_statuses=("ACTIVE",),
    reservation_treatment=(
        "Only active, unexpired reservations reduce available stock."
    ),
    branch_meaning="A customer-facing sales location.",
    warehouse_meaning="A stock-holding location that does not record retail sales.",
    quantity_interpretation=(
        "Available quantity equals on-hand quantity minus valid reservations, "
        "bounded at zero."
    ),
    revenue_interpretation=(
        "Gross line totals minus completed refund amounts in the requested "
        "source-local period."
    ),
    currency="LBP",
    source_timezone="Asia/Beirut",
    required_capabilities=OPERATIONAL_CAPABILITIES,
    source_location_types={
        "branch": ("BRANCH",),
        "warehouse": ("WAREHOUSE",),
    },
)

FAKE_STORE_PROFILE = ConnectionProfile(
    key=FAKE_STORE_CONNECTION_PROFILE,
    display_name="PostgreSQL Demo Store",
    description=(
        "Read-only connection to the local Lebanese minimarket demonstration source."
    ),
    adapter_type=POSTGRESQL_READONLY_ADAPTER,
    mapping_profile_key=FAKE_STORE_MAPPING_PROFILE,
    mapping_profile_version=FAKE_STORE_MAPPING_VERSION,
)


@runtime_checkable
class ConnectionProfileRegistry(Protocol):
    """Replaceable resolver for deployment-managed source credentials."""

    def available_profiles(self) -> tuple[ConnectionProfile, ...]: ...

    def get_profile(self, key: str) -> ConnectionProfile | None: ...

    def get_mapping(
        self, key: str, version: int
    ) -> OperationalMappingProfile | None: ...

    def resolve(self, key: str) -> OperationalDataSource: ...

    def connector(self, key: str, business_id: uuid.UUID) -> EngineConnector: ...


class EnvironmentConnectionProfileRegistry:
    """Resolve safe profile keys from deployment environment configuration."""

    def __init__(self, settings: Settings) -> None:
        FAKE_STORE_MAPPING.validate_definition()
        self._source = create_fake_store_adapter(
            settings, location_type_mapping=FAKE_STORE_MAPPING.source_location_types
        )
        try:
            configurations = TypeAdapter(list[SourceConnection]).validate_json(
                settings.source_connections_json.get_secret_value()
            )
        except ValidationError:
            raise ValueError(
                "Invalid source connection policy; check private configuration."
            ) from None
        if len({item.key for item in configurations}) != len(configurations) or any(
            item.key == FAKE_STORE_PROFILE.key for item in configurations
        ):
            raise ValueError("Duplicate source connection profile.")
        self._configurations = {item.key: item for item in configurations}
        self._connectors: dict[str, EngineConnector] = {}
        self._connector_lock = Lock()
        self._profiles = tuple(
            ConnectionProfile(
                key=item.key,
                display_name=item.display_name,
                description="Restricted catalogue discovery; approval required.",
                adapter_type=f"{item.engine}_readonly",
                mapping_profile_key="discovered_products",
                mapping_profile_version=1,
                business_ids=item.business_ids,
            )
            for item in configurations
        )

    def available_profiles(self) -> tuple[ConnectionProfile, ...]:
        return (FAKE_STORE_PROFILE, *self._profiles)

    def get_profile(self, key: str) -> ConnectionProfile | None:
        return next(
            (item for item in self.available_profiles() if item.key == key), None
        )

    def connector(self, key: str, business_id: uuid.UUID) -> EngineConnector:
        config = self._configurations.get(key)
        if config is None or not config.permits(business_id):
            raise MappingProfileError("Operational connection is not authorized.")
        with self._connector_lock:
            if key not in self._connectors:
                self._connectors[key] = EngineConnector(config)
            return self._connectors[key]

    def get_mapping(self, key: str, version: int) -> OperationalMappingProfile | None:
        if key == FAKE_STORE_MAPPING.key and version == FAKE_STORE_MAPPING.version:
            return FAKE_STORE_MAPPING
        return None

    def resolve(self, key: str) -> OperationalDataSource:
        if key != FAKE_STORE_PROFILE.key:
            raise KeyError("Unsupported operational connection profile.")
        return self._source


@lru_cache
def get_connection_profile_registry() -> ConnectionProfileRegistry:
    return EnvironmentConnectionProfileRegistry(get_settings())
