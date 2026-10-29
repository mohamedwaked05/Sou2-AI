"""Fixed read-only historical projections for the isolated Hilaleyih demo.

No source write or approval operation is available here. The fixed financial
projection is separately authorized by the development-only test-mode switch;
SQL Server permissions remain authoritative.
"""

from datetime import date
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import text
from sqlalchemy.engine import RowMapping, make_url

from app.core.config import Settings
from app.integrations.discovery import EngineConnector, SourceMappingError

BUSINESS_ID = "e80f60e9-9636-40d8-8a98-914304a97dc1"
SOURCE_ID = "c95aea88-6b60-4720-9548-d1f3492f7388"


def stable_quantity(product_id: str, location: str) -> int:
    identity = f"hilaleyih-stock-v1\0{product_id}\0{location}".encode()
    return int.from_bytes(sha256(identity).digest()[:8], "big") % 201


def estimated_price(product_id: str) -> Decimal:
    identity = f"hilaleyih-price-v1\0{product_id}".encode()
    cents = 50 + int.from_bytes(sha256(identity).digest()[:8], "big") % 1951
    return Decimal(cents) / 100


class HilaleyihHistoricalReader:
    def __init__(self, connector: EngineConnector, settings: Settings):
        if not (
            settings.hilaleyih_test_mode
            and settings.environment.casefold() == "development"
        ):
            raise SourceMappingError("hilaleyih_test_disabled")
        self.connector = connector
        if make_url(connector.config.url.get_secret_value()).database != (
            "sou2ai_hila23_test"
        ):
            raise SourceMappingError("hilaleyih_test_database_required")

    def _check_scope(self, table: str, columns: set[str]) -> None:
        config = self.connector.config
        if (
            config.key != "hila_catalogue_dev"
            or config.engine != "sqlserver"
            or tuple(map(str, config.business_ids)) != (BUSINESS_ID,)
            or not any(
                scope.schema_name == "dbo"
                and scope.object_name == table
                and columns <= set(scope.columns)
                for scope in config.discovery_scope
            )
        ):
            raise SourceMappingError("hilaleyih_test_scope_required")

    def price(self, product_id: str) -> RowMapping | None:
        self._check_scope("ItemCard", {"Code", "Selling1", "ItemCurr", "Unit"})
        with self.connector.connection() as connection:
            return (
                connection.execute(
                    text(
                        "SELECT Selling1, ItemCurr, Unit FROM dbo.ItemCard "
                        "WHERE Code=:product"
                    ),
                    {"product": product_id},
                )
                .mappings()
                .one_or_none()
            )

    def financial_summary(self, start: date, end: date) -> list[RowMapping]:
        # This fixed development-only projection is a separate read policy. Do
        # not change the approved catalogue connection fingerprint or mapping.
        self._check_scope(
            "SalesHeader",
            {
                "CompCode",
                "Type",
                "Status",
                "Date",
            },
        )
        with self.connector.connection(consistent=True) as connection:
            can_reconcile = all(
                connection.scalar(
                    text(
                        "SELECT HAS_PERMS_BY_NAME('dbo.SalesHeader', 'OBJECT', "
                        "'SELECT', :column, 'COLUMN')"
                    ),
                    {"column": column},
                )
                == 1
                for column in ("AmountPaid", "AmountRest")
            )
            settlement = (
                "SUM(CASE WHEN ABS(AmountPaid+AmountRest-"
                "(GrossTotal-DiscAMT+VATAMT))>0.01 THEN 1 ELSE 0 END) "
                "AS settlement_discrepancies, "
                "SUM(AmountPaid+AmountRest-(GrossTotal-DiscAMT+VATAMT)) "
                "AS settlement_difference"
                if can_reconcile
                else "NULL AS settlement_discrepancies, NULL AS settlement_difference"
            )
            return (
                connection.execute(
                    text(
                        """
                    SELECT Curr AS currency_code, COUNT_BIG(*) AS documents,
                        SUM(GrossTotal) AS gross, SUM(DiscAMT) AS discounts,
                        SUM(VATAMT) AS taxes,
                        SUM(GrossTotal-DiscAMT+VATAMT) AS signed_total,
                        SUM(CASE WHEN GrossTotal<0 THEN 1 ELSE 0 END)
                            AS negative_documents,
                        SUM(CASE WHEN GrossTotal<0
                            THEN GrossTotal-DiscAMT+VATAMT ELSE 0 END)
                            AS negative_total,
                        SUM(CASE WHEN GrossTotal IS NULL OR DiscAMT IS NULL
                            OR VATAMT IS NULL THEN 1 ELSE 0 END)
                            AS incomplete_documents,
                        """
                        + settlement
                        + """
                    FROM dbo.SalesHeader
                    WHERE CompCode=:company AND Type=:type AND Status=:status
                        AND Date>=:start AND Date<:end
                    GROUP BY Curr ORDER BY Curr
                """
                    ),
                    {
                        "company": "hila23",
                        "type": "SI",
                        "status": "Paid",
                        "start": start,
                        "end": end,
                    },
                )
                .mappings()
                .all()
            )
