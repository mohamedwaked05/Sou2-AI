"""Fixed read-only historical projections for the isolated Hilaleyih demo.

No source write or approval operation is available here. The fixed financial
projection is separately authorized by the development-only test-mode switch;
SQL Server permissions remain authoritative.
"""

from datetime import date
from decimal import ROUND_CEILING, Decimal
from hashlib import sha256

from sqlalchemy import text
from sqlalchemy.engine import RowMapping, make_url

from app.core.config import Settings
from app.integrations.discovery import EngineConnector, SourceMappingError

BUSINESS_ID = "e80f60e9-9636-40d8-8a98-914304a97dc1"
SOURCE_ID = "c95aea88-6b60-4720-9548-d1f3492f7388"


def demand_proxy(quantity: Decimal, calendar_days: int) -> Decimal | None:
    """Model one seven-day replenishment target, never an observed balance."""
    if not quantity.is_finite() or quantity <= 0 or calendar_days <= 0:
        return None
    return (quantity * 7 / calendar_days).to_integral_value(rounding=ROUND_CEILING)


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

    @staticmethod
    def _readable(connection, table: str, columns: tuple[str, ...]) -> bool:
        return all(
            connection.scalar(
                text(
                    "SELECT HAS_PERMS_BY_NAME(:table, 'OBJECT', 'SELECT', "
                    ":column, 'COLUMN')"
                ),
                {"table": f"dbo.{table}", "column": column},
            )
            == 1
            for column in columns
        )

    def inventory(self, product_id: str) -> dict[str, object]:
        """Single-store historical balances or traceable positive sales demand.

        IDBranch appears only in the source's composite document join. It does
        not identify physical stores and is never a reporting filter/group.
        """
        self._check_scope("ItemCard", {"Code", "Unit"})
        self._check_scope("SalesHeader", {"CompCode", "Type", "Status", "Date"})
        with self.connector.connection(consistent=True) as connection:
            unit = connection.scalar(
                text("SELECT Unit FROM dbo.ItemCard WHERE Code=:product"),
                {"product": product_id},
            )
            balance = None
            balance_reason = (
                "No reliable matched historical balance is available. "
                "ItemsQty lacks company, unit and balance-date evidence; "
                "ItemsInventory buckets do not define an on-hand balance."
            )
            if self._readable(
                connection,
                "StockInventory",
                (
                    "Code",
                    "CompCode",
                    "WH",
                    "Unit",
                    "Balance",
                ),
            ):
                balances = (
                    connection.execute(
                        text("""
                    SELECT WH, Unit, Balance FROM dbo.StockInventory
                    WHERE Code=:product AND CompCode=:company
                """),
                        {"product": product_id, "company": "hila23"},
                    )
                    .mappings()
                    .all()
                )
                if (
                    balances
                    and unit
                    and str(unit).strip()
                    and len({row["WH"] for row in balances}) == len(balances)
                    and all(
                        row["WH"] is not None
                        and str(row["Unit"] or "").strip().casefold()
                        == str(unit).strip().casefold()
                        and row["Balance"] is not None
                        and Decimal(str(row["Balance"])).is_finite()
                        for row in balances
                    )
                ):
                    balance = sum(Decimal(str(row["Balance"])) for row in balances)
                    return {
                        "unit": str(unit).strip(),
                        "balance": balance,
                        "balance_records": len(balances),
                        "balance_reason": "",
                    }
            else:
                balance_reason += " StockInventory reads are denied by SQL Server."
            if not self._readable(
                connection,
                "SalesFooter",
                (
                    "Type",
                    "InvNumber",
                    "CompCode",
                    "IDBranch",
                    "Recno",
                    "ItemCode",
                    "Qty",
                    "FreeQty",
                    "PackUnit",
                    "QtyInPack",
                    "PackUnitFree",
                    "FQtyInPack",
                ),
            ):
                raise SourceMappingError("historical_quantity_read_denied")
            # Only explicit base-unit lines are used; unknown pack conversion is
            # never guessed. Negative documents are excluded, not called refunds.
            rows = connection.execute(
                text("""
                SELECT f.Type,f.InvNumber,f.Recno,f.CompCode,f.IDBranch,
                    f.Qty,f.FreeQty,f.PackUnit,f.QtyInPack,
                    f.PackUnitFree,f.FQtyInPack,h.GrossTotal,h.Date AS DocumentDate
                FROM dbo.SalesFooter f JOIN dbo.SalesHeader h
                    ON h.Type=f.Type AND h.InvNumber=f.InvNumber
                    AND h.CompCode=f.CompCode AND h.IDBranch=f.IDBranch
                WHERE h.CompCode=:company AND h.Type=:type AND h.Status=:status
                    AND f.ItemCode=:product
                ORDER BY h.Date,f.InvNumber,f.Recno
            """),
                {
                    "company": "hila23",
                    "type": "SI",
                    "status": "Paid",
                    "product": product_id,
                },
            ).mappings()
            # Aggregation streams bounded historical rows under the source
            # timeout. Only three record references leave this method.
            result: dict[str, object] = {
                "unit": str(unit or "").strip(),
                "balance": None,
                "balance_reason": balance_reason,
                "line_count": 0,
                "usable_lines": 0,
                "demand_quantity": Decimal(0),
                "negative_lines": 0,
                "negative_quantity": Decimal(0),
                "unusable_lines": 0,
                "records": [],
            }
            for row in rows:
                result["line_count"] += 1
                values = (row["Qty"], row["FreeQty"], row["GrossTotal"])
                if row["DocumentDate"] is None or any(
                    v is None or not Decimal(str(v)).is_finite() for v in values
                ):
                    result["unusable_lines"] += 1
                    continue
                qty, free, gross = (Decimal(str(v)) for v in values)
                if qty < 0 or free < 0 or gross < 0:
                    result["negative_lines"] += 1
                    result["negative_quantity"] += qty + free
                    continue
                valid_unit = (
                    bool(result["unit"])
                    and str(row["PackUnit"] or "").strip().casefold()
                    == result["unit"].casefold()
                    and row["QtyInPack"] == 1
                    and (
                        free == 0
                        or (
                            str(row["PackUnitFree"] or "").strip().casefold()
                            == result["unit"].casefold()
                            and row["FQtyInPack"] == 1
                        )
                    )
                )
                if not valid_unit:
                    result["unusable_lines"] += 1
                    continue
                result["usable_lines"] += 1
                result["demand_quantity"] += qty + free
                if len(result["records"]) < 3:
                    result["records"].append(
                        "/".join(
                            str(row[k])
                            for k in (
                                "Type",
                                "InvNumber",
                                "Recno",
                                "CompCode",
                                "IDBranch",
                            )
                        )
                    )
            period = (
                connection.execute(
                    text("""
                SELECT CAST(MIN(Date) AS date) first_date,
                    CAST(MAX(Date) AS date) last_date,
                    DATEDIFF(day,MIN(Date),MAX(Date))+1 calendar_days
                FROM dbo.SalesHeader
                WHERE CompCode=:company AND Type=:type AND Status=:status
            """),
                    {"company": "hila23", "type": "SI", "status": "Paid"},
                )
                .mappings()
                .one()
            )
            result.update(period)
            result["orphan_lines"] = connection.scalar(
                text("""
                SELECT COUNT_BIG(*) FROM dbo.SalesFooter f
                WHERE f.CompCode=:company AND f.Type=:type AND f.ItemCode=:product
                    AND NOT EXISTS (SELECT 1 FROM dbo.SalesHeader h
                        WHERE h.Type=f.Type AND h.InvNumber=f.InvNumber
                        AND h.CompCode=f.CompCode AND h.IDBranch=f.IDBranch)
            """),
                {"company": "hila23", "type": "SI", "product": product_id},
            )
            return result

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
