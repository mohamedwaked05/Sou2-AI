"""Read-only PostgreSQL adapter for the external demonstration store."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, time
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.core.config import Settings, get_settings
from app.integrations.operational import (
    OperationalDataInvalid,
    OperationalQueryTimeout,
    OperationalSourceUnavailable,
)
from app.schemas.operational import (
    BestSellersQuery,
    BestSellingProduct,
    BestSellingProductsResult,
    IntegrationHealth,
    InventoryItem,
    InventoryQuery,
    InventoryResult,
    OperationalResultMetadata,
    Product,
    ReportingPeriod,
    RestockingQuery,
    RestockingRecommendation,
    RestockingRecommendationsResult,
    SalesQuery,
    SalesSummary,
)

_SOURCE_CONFIGURATION_SQL = text(
    """
    SELECT business_timezone, currency_code, data_updated_at
    FROM minimarket.store_configuration
    WHERE singleton = true
    """
)

_INVENTORY_SQL = text(
    r"""
    WITH valid_reservations AS (
        SELECT location_id, item_id, SUM(held_quantity) AS reserved_quantity
        FROM minimarket.stock_reservations
        WHERE reservation_state = 'ACTIVE'
          AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
        GROUP BY location_id, item_id
    ), normalized_stock AS (
        SELECT
            item.item_code AS external_product_id,
            item.merchant_sku AS sku,
            item.ean_barcode AS barcode,
            item.display_label AS product_name,
            category.label AS category,
            location.location_code,
            location.location_label,
            location.location_type,
            stock.physical_quantity AS on_hand_quantity,
            COALESCE(reservation.reserved_quantity, 0) AS reserved_quantity,
            GREATEST(
                stock.physical_quantity
                    - COALESCE(reservation.reserved_quantity, 0),
                0
            ) AS available_quantity,
            stock.reorder_threshold AS reorder_point,
            stock.desired_quantity AS target_stock
        FROM minimarket.stock_levels AS stock
        JOIN minimarket.catalog_items AS item ON item.item_id = stock.item_id
        LEFT JOIN minimarket.categories AS category
            ON category.category_id = item.category_id
        JOIN minimarket.stock_locations AS location
            ON location.location_id = stock.location_id
        LEFT JOIN valid_reservations AS reservation
            ON reservation.location_id = stock.location_id
           AND reservation.item_id = stock.item_id
        WHERE item.active = true
          AND (
              CAST(:product_filter AS text) IS NULL
              OR item.display_label ILIKE :product_filter ESCAPE '\'
              OR item.item_code ILIKE :product_filter ESCAPE '\'
              OR item.merchant_sku ILIKE :product_filter ESCAPE '\'
              OR item.ean_barcode ILIKE :product_filter ESCAPE '\'
          )
          AND (
              CAST(:branch_code AS text) IS NULL
              OR (
                  location.location_type = 'BRANCH'
                  AND location.location_code = :branch_code
              )
          )
          AND (
              CAST(:warehouse_code AS text) IS NULL
              OR (
                  location.location_type = 'WAREHOUSE'
                  AND location.location_code = :warehouse_code
              )
          )
    )
    SELECT *
    FROM normalized_stock
    WHERE CAST(:restock_only AS boolean) = false
       OR (
           available_quantity <= reorder_point
           AND target_stock > available_quantity
       )
    ORDER BY
        CASE location_type WHEN 'BRANCH' THEN 0 ELSE 1 END,
        location_code,
        product_name,
        external_product_id
    LIMIT :row_limit
    """
)

_SALES_SUMMARY_SQL = text(
    """
    WITH filtered_sales AS (
        SELECT receipt_id, receipt_state
        FROM minimarket.receipts
        WHERE receipt_state IN ('COMPLETED', 'RETURNED')
          AND completed_at >= :start_at
          AND completed_at < :end_at
          AND (
              CAST(:branch_code AS text) IS NULL
              OR branch_location_id = (
                  SELECT location_id
                  FROM minimarket.stock_locations
                  WHERE location_type = 'BRANCH'
                    AND location_code = :branch_code
              )
          )
    ), sale_totals AS (
        SELECT
            COUNT(DISTINCT sale.receipt_id)
                FILTER (WHERE sale.receipt_state = 'COMPLETED') AS completed_count,
            COUNT(DISTINCT sale.receipt_id)
                FILTER (WHERE sale.receipt_state = 'RETURNED') AS returned_count,
            COALESCE(SUM(line.sold_quantity), 0) AS gross_quantity,
            COALESCE(SUM(line.line_total), 0) AS gross_revenue
        FROM filtered_sales AS sale
        LEFT JOIN minimarket.receipt_lines AS line
            ON line.receipt_id = sale.receipt_id
    ), filtered_refunds AS (
        SELECT refund.refund_id
        FROM minimarket.refunds AS refund
        JOIN minimarket.receipts AS receipt
            ON receipt.receipt_id = refund.receipt_id
        WHERE refund.refund_state = 'COMPLETED'
          AND refund.refunded_at >= :start_at
          AND refund.refunded_at < :end_at
          AND (
              CAST(:branch_code AS text) IS NULL
              OR receipt.branch_location_id = (
                  SELECT location_id
                  FROM minimarket.stock_locations
                  WHERE location_type = 'BRANCH'
                    AND location_code = :branch_code
              )
          )
    ), refund_totals AS (
        SELECT
            COUNT(DISTINCT refund.refund_id) AS refund_count,
            COALESCE(SUM(line.returned_quantity), 0) AS returned_quantity,
            COALESCE(SUM(line.refund_total), 0) AS refund_amount
        FROM filtered_refunds AS refund
        LEFT JOIN minimarket.refund_lines AS line
            ON line.refund_id = refund.refund_id
    )
    SELECT
        sale.completed_count,
        sale.returned_count,
        refund.refund_count,
        sale.gross_quantity,
        refund.returned_quantity,
        sale.gross_revenue,
        refund.refund_amount
    FROM sale_totals AS sale
    CROSS JOIN refund_totals AS refund
    """
)

_BEST_SELLERS_SQL = text(
    """
    WITH sale_by_product AS (
        SELECT
            line.item_id,
            SUM(line.sold_quantity) AS gross_quantity,
            SUM(line.line_total) AS gross_revenue
        FROM minimarket.receipts AS receipt
        JOIN minimarket.receipt_lines AS line
            ON line.receipt_id = receipt.receipt_id
        WHERE receipt.receipt_state IN ('COMPLETED', 'RETURNED')
          AND receipt.completed_at >= :start_at
          AND receipt.completed_at < :end_at
          AND (
              CAST(:branch_code AS text) IS NULL
              OR receipt.branch_location_id = (
                  SELECT location_id
                  FROM minimarket.stock_locations
                  WHERE location_type = 'BRANCH'
                    AND location_code = :branch_code
              )
          )
        GROUP BY line.item_id
    ), refund_by_product AS (
        SELECT
            receipt_line.item_id,
            SUM(refund_line.returned_quantity) AS returned_quantity,
            SUM(refund_line.refund_total) AS refund_amount
        FROM minimarket.refunds AS refund
        JOIN minimarket.receipts AS receipt
            ON receipt.receipt_id = refund.receipt_id
        JOIN minimarket.refund_lines AS refund_line
            ON refund_line.refund_id = refund.refund_id
        JOIN minimarket.receipt_lines AS receipt_line
            ON receipt_line.receipt_line_id = refund_line.receipt_line_id
        WHERE refund.refund_state = 'COMPLETED'
          AND refund.refunded_at >= :start_at
          AND refund.refunded_at < :end_at
          AND (
              CAST(:branch_code AS text) IS NULL
              OR receipt.branch_location_id = (
                  SELECT location_id
                  FROM minimarket.stock_locations
                  WHERE location_type = 'BRANCH'
                    AND location_code = :branch_code
              )
          )
        GROUP BY receipt_line.item_id
    ), ranked_source AS (
        SELECT
            COALESCE(sale.item_id, refund.item_id) AS item_id,
            COALESCE(sale.gross_quantity, 0)
                - COALESCE(refund.returned_quantity, 0) AS net_quantity,
            COALESCE(sale.gross_revenue, 0)
                - COALESCE(refund.refund_amount, 0) AS net_revenue
        FROM sale_by_product AS sale
        FULL OUTER JOIN refund_by_product AS refund
            ON refund.item_id = sale.item_id
    )
    SELECT
        item.item_code AS external_product_id,
        item.merchant_sku AS sku,
        item.ean_barcode AS barcode,
        item.display_label AS product_name,
        category.label AS category,
        ranked.net_quantity,
        ranked.net_revenue
    FROM ranked_source AS ranked
    JOIN minimarket.catalog_items AS item ON item.item_id = ranked.item_id
    LEFT JOIN minimarket.categories AS category
        ON category.category_id = item.category_id
    WHERE ranked.net_quantity > 0
    ORDER BY
        ranked.net_quantity DESC,
        ranked.net_revenue DESC,
        item.item_code ASC
    LIMIT :row_limit
    """
)


def _reject_privileged_operational_connection(
    dbapi_connection: object, _connection_record: object
) -> None:
    """Fail closed unless the source login is an unprivileged read-only role."""
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute(
            """
            SELECT
                role.rolsuper
                OR role.rolcreatedb
                OR role.rolcreaterole
                OR role.rolreplication
                OR role.rolbypassrls,
                current_setting('transaction_read_only') = 'on'
            FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = current_user
            """
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
    if row is None or row[0] or not row[1]:
        raise RuntimeError(
            "Operational adapters require an unprivileged read-only PostgreSQL role."
        )


class PostgreSQLOperationalAdapter:
    """Normalize predefined fake-store queries into Sou2AI contracts."""

    def __init__(
        self,
        database_url: SecretStr | str | None = None,
        *,
        connect_timeout_seconds: int = 5,
        query_timeout_seconds: int = 2,
        max_reporting_days: int = 366,
        engine: Engine | None = None,
    ) -> None:
        if query_timeout_seconds < 1 or query_timeout_seconds > 30:
            raise ValueError(
                "Operational query timeout must be between 1 and 30 seconds."
            )
        if max_reporting_days < 1 or max_reporting_days > 366:
            raise ValueError(
                "Operational reporting range must be between 1 and 366 days."
            )
        self.query_timeout_milliseconds = query_timeout_seconds * 1000
        self.max_reporting_days = max_reporting_days
        if engine is not None:
            self._engine = engine
            return
        if database_url is None:
            raise ValueError("An operational database URL is required.")
        url = (
            database_url.get_secret_value()
            if isinstance(database_url, SecretStr)
            else database_url
        )
        self._engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={
                "connect_timeout": connect_timeout_seconds,
                "options": (
                    "-c default_transaction_read_only=on "
                    f"-c statement_timeout={self.query_timeout_milliseconds} "
                    "-c search_path=pg_catalog"
                ),
            },
        )
        event.listen(self._engine, "connect", _reject_privileged_operational_connection)

    def get_current_inventory(self, query: InventoryQuery) -> InventoryResult:
        try:
            with self._engine.connect() as connection:
                self._prepare_read(connection)
                source = self._source_configuration(connection)
                rows = self._inventory_rows(connection, query, restock_only=False)
            items = tuple(self._normalize_inventory(row) for row in rows[: query.limit])
            return InventoryResult(
                items=items,
                metadata=self._metadata(
                    source,
                    row_count=len(items),
                    requested_limit=query.limit,
                    is_truncated=len(rows) > query.limit,
                ),
            )
        except (SQLAlchemyError, RuntimeError) as exc:
            self._raise_safe_database_error(exc)
        except ValidationError, KeyError, TypeError, ArithmeticError:
            raise OperationalDataInvalid(
                "Operational source data is invalid."
            ) from None

    def get_sales_summary(self, query: SalesQuery) -> SalesSummary:
        try:
            with self._engine.connect() as connection:
                self._prepare_read(connection)
                source = self._source_configuration(connection)
                period, start_at, end_at = self._reporting_bounds(query, source)
                row = (
                    connection.execute(
                        _SALES_SUMMARY_SQL,
                        {
                            "start_at": start_at,
                            "end_at": end_at,
                            "branch_code": query.branch_external_id,
                        },
                    )
                    .mappings()
                    .one()
                )
            gross_quantity = row["gross_quantity"]
            returned_quantity = row["returned_quantity"]
            gross_revenue = row["gross_revenue"]
            refund_amount = row["refund_amount"]
            return SalesSummary(
                period=period,
                branch_external_id=query.branch_external_id,
                completed_sale_count=row["completed_count"],
                returned_sale_count=row["returned_count"],
                completed_refund_count=row["refund_count"],
                gross_quantity_sold=gross_quantity,
                returned_quantity=returned_quantity,
                net_quantity_sold=gross_quantity - returned_quantity,
                gross_revenue=gross_revenue,
                refund_amount=refund_amount,
                net_revenue=gross_revenue - refund_amount,
                currency=source["currency_code"],
                metadata=self._metadata(source, row_count=1),
            )
        except (SQLAlchemyError, RuntimeError) as exc:
            self._raise_safe_database_error(exc)
        except ValidationError, KeyError, TypeError, ArithmeticError:
            raise OperationalDataInvalid(
                "Operational source data is invalid."
            ) from None

    def get_best_selling_products(
        self, query: BestSellersQuery
    ) -> BestSellingProductsResult:
        try:
            with self._engine.connect() as connection:
                self._prepare_read(connection)
                source = self._source_configuration(connection)
                period, start_at, end_at = self._reporting_bounds(query, source)
                rows = list(
                    connection.execute(
                        _BEST_SELLERS_SQL,
                        {
                            "start_at": start_at,
                            "end_at": end_at,
                            "branch_code": query.branch_external_id,
                            "row_limit": query.limit + 1,
                        },
                    ).mappings()
                )
            items = tuple(
                BestSellingProduct(
                    rank=index,
                    product=self._normalize_product(row),
                    quantity_sold=row["net_quantity"],
                    revenue=row["net_revenue"],
                    currency=source["currency_code"],
                )
                for index, row in enumerate(rows[: query.limit], start=1)
            )
            return BestSellingProductsResult(
                period=period,
                branch_external_id=query.branch_external_id,
                items=items,
                metadata=self._metadata(
                    source,
                    row_count=len(items),
                    requested_limit=query.limit,
                    is_truncated=len(rows) > query.limit,
                ),
            )
        except (SQLAlchemyError, RuntimeError) as exc:
            self._raise_safe_database_error(exc)
        except ValidationError, KeyError, TypeError, ArithmeticError:
            raise OperationalDataInvalid(
                "Operational source data is invalid."
            ) from None

    def get_restocking_recommendations(
        self, query: RestockingQuery
    ) -> RestockingRecommendationsResult:
        try:
            with self._engine.connect() as connection:
                self._prepare_read(connection)
                source = self._source_configuration(connection)
                rows = self._inventory_rows(connection, query, restock_only=True)
            inventory_items = tuple(
                self._normalize_inventory(row) for row in rows[: query.limit]
            )
            items = tuple(
                RestockingRecommendation(
                    inventory=inventory,
                    recommended_quantity=(
                        inventory.target_stock - inventory.available_quantity
                    ),
                )
                for inventory in inventory_items
            )
            return RestockingRecommendationsResult(
                items=items,
                metadata=self._metadata(
                    source,
                    row_count=len(items),
                    requested_limit=query.limit,
                    is_truncated=len(rows) > query.limit,
                ),
            )
        except (SQLAlchemyError, RuntimeError) as exc:
            self._raise_safe_database_error(exc)
        except ValidationError, KeyError, TypeError, ArithmeticError:
            raise OperationalDataInvalid(
                "Operational source data is invalid."
            ) from None

    def check_health(self) -> IntegrationHealth:
        checked_at = datetime.now(UTC)
        try:
            with self._engine.connect() as connection:
                self._prepare_read(connection)
                connection.execute(text("SELECT 1")).scalar_one()
                source = self._source_configuration(connection)
            return IntegrationHealth(
                status="healthy",
                checked_at=checked_at,
                source_timezone=source["business_timezone"],
                currency=source["currency_code"],
                data_timestamp=source["data_updated_at"],
            )
        except SQLAlchemyError, RuntimeError, ValidationError, KeyError, TypeError:
            return IntegrationHealth(
                status="unavailable",
                checked_at=checked_at,
                error_code="operational_source_unavailable",
            )

    def dispose(self) -> None:
        """Release pooled connections without changing source data."""
        self._engine.dispose()

    def _prepare_read(self, connection: Connection) -> None:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        connection.execute(
            text("SELECT set_config('statement_timeout', :timeout, true)"),
            {"timeout": f"{self.query_timeout_milliseconds}ms"},
        )

    @staticmethod
    def _source_configuration(connection: Connection) -> Mapping[str, Any]:
        return connection.execute(_SOURCE_CONFIGURATION_SQL).mappings().one()

    def _inventory_rows(
        self,
        connection: Connection,
        query: InventoryQuery,
        *,
        restock_only: bool,
    ) -> list[Mapping[str, Any]]:
        return list(
            connection.execute(
                _INVENTORY_SQL,
                {
                    "product_filter": self._escaped_search(query.product_filter),
                    "branch_code": query.branch_external_id,
                    "warehouse_code": query.warehouse_external_id,
                    "restock_only": restock_only,
                    "row_limit": query.limit + 1,
                },
            ).mappings()
        )

    def _reporting_bounds(
        self, query: SalesQuery, source: Mapping[str, Any]
    ) -> tuple[ReportingPeriod, datetime, datetime]:
        period = query.period(source["business_timezone"])
        if (period.end_date - period.start_date).days > self.max_reporting_days:
            raise OperationalDataInvalid(
                "Reporting period exceeds the configured maximum."
            )
        timezone = ZoneInfo(period.source_timezone)
        start_at = datetime.combine(period.start_date, time.min, timezone).astimezone(
            UTC
        )
        end_at = datetime.combine(period.end_date, time.min, timezone).astimezone(UTC)
        return period, start_at, end_at

    @staticmethod
    def _normalize_product(row: Mapping[str, Any]) -> Product:
        return Product(
            external_product_id=row["external_product_id"],
            sku=row["sku"],
            barcode=row["barcode"],
            name=row["product_name"],
            category=row["category"],
        )

    @classmethod
    def _normalize_inventory(cls, row: Mapping[str, Any]) -> InventoryItem:
        is_branch = row["location_type"] == "BRANCH"
        return InventoryItem(
            product=cls._normalize_product(row),
            branch_external_id=row["location_code"] if is_branch else None,
            branch_name=row["location_label"] if is_branch else None,
            warehouse_external_id=row["location_code"] if not is_branch else None,
            warehouse_name=row["location_label"] if not is_branch else None,
            on_hand_quantity=row["on_hand_quantity"],
            reserved_quantity=row["reserved_quantity"],
            available_quantity=row["available_quantity"],
            reorder_point=row["reorder_point"],
            target_stock=row["target_stock"],
        )

    @staticmethod
    def _escaped_search(value: str | None) -> str | None:
        if value is None:
            return None
        escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    @staticmethod
    def _metadata(
        source: Mapping[str, Any],
        *,
        row_count: int,
        requested_limit: int | None = None,
        is_truncated: bool = False,
    ) -> OperationalResultMetadata:
        queried_at = datetime.now(UTC)
        data_timestamp = source["data_updated_at"]
        freshness_seconds = max(
            0, int((queried_at - data_timestamp.astimezone(UTC)).total_seconds())
        )
        return OperationalResultMetadata(
            source_timezone=source["business_timezone"],
            data_timestamp=data_timestamp,
            queried_at=queried_at,
            freshness_seconds=freshness_seconds,
            row_count=row_count,
            requested_limit=requested_limit,
            is_truncated=is_truncated,
        )

    @staticmethod
    def _raise_safe_database_error(exc: BaseException) -> None:
        if PostgreSQLOperationalAdapter._is_query_timeout(exc):
            raise OperationalQueryTimeout(
                "Operational source query timed out."
            ) from None
        raise OperationalSourceUnavailable(
            "Operational source is unavailable."
        ) from None

    @staticmethod
    def _is_query_timeout(exc: BaseException) -> bool:
        candidates: list[object] = [exc]
        if isinstance(exc, DBAPIError):
            candidates.append(exc.orig)
        return any(
            getattr(candidate, "sqlstate", None) == "57014"
            or getattr(candidate, "pgcode", None) == "57014"
            for candidate in candidates
        )


def create_fake_store_adapter(settings: Settings) -> PostgreSQLOperationalAdapter:
    """Build the configured adapter without exposing its secret connection URL."""
    return PostgreSQLOperationalAdapter(
        settings.fake_store_database_url,
        connect_timeout_seconds=settings.postgresql_connect_timeout_seconds,
        query_timeout_seconds=settings.operational_query_timeout_seconds,
        max_reporting_days=settings.operational_max_reporting_days,
    )


@lru_cache
def get_fake_store_adapter() -> PostgreSQLOperationalAdapter:
    return create_fake_store_adapter(get_settings())
