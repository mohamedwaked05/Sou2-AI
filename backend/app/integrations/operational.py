"""Provider-neutral boundary for predefined read-only operational operations."""

from typing import Protocol, runtime_checkable

from app.schemas.operational import (
    BestSellersQuery,
    BestSellingProductsResult,
    IntegrationHealth,
    InventoryQuery,
    InventoryResult,
    RestockingQuery,
    RestockingRecommendationsResult,
    SalesQuery,
    SalesSummary,
)


class OperationalIntegrationError(Exception):
    """Safe base error that never contains source-system details."""

    code = "operational_integration_error"


class OperationalSourceUnavailable(OperationalIntegrationError):
    code = "operational_source_unavailable"


class OperationalQueryTimeout(OperationalIntegrationError):
    code = "operational_query_timeout"


class OperationalDataInvalid(OperationalIntegrationError):
    code = "operational_data_invalid"


@runtime_checkable
class OperationalDataSource(Protocol):
    """Operations supported by a structured live operational source."""

    def get_current_inventory(self, query: InventoryQuery) -> InventoryResult: ...

    def get_sales_summary(self, query: SalesQuery) -> SalesSummary: ...

    def get_best_selling_products(
        self, query: BestSellersQuery
    ) -> BestSellingProductsResult: ...

    def get_restocking_recommendations(
        self, query: RestockingQuery
    ) -> RestockingRecommendationsResult: ...

    def check_health(self) -> IntegrationHealth: ...
