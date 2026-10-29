"""Historical stock preference, strict quantity evidence and demand arithmetic."""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.integrations.discovery import DiscoveryScope, SourceConnection
from app.integrations.hilaleyih_test import (
    BUSINESS_ID,
    HilaleyihHistoricalReader,
    demand_proxy,
)
from app.services.hilaleyih_test import render_inventory
from pydantic import SecretStr


class Rows(list):
    def all(self):
        return list(self)

    def mappings(self):
        return self

    def one(self):
        assert len(self) == 1
        return self[0]


def reader_for(lines, unit="pc", balances=None):
    class Connector:
        config = SourceConnection(
            key="hila_catalogue_dev",
            display_name="Synthetic fixture",
            engine="sqlserver",
            url=SecretStr(
                "mssql+pyodbc://localhost/sou2ai_hila23_test?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes"
            ),
            business_ids=(BUSINESS_ID,),
            discovery_scope=(
                DiscoveryScope(
                    schema_name="dbo", object_name="ItemCard", columns=("Code", "Unit")
                ),
                DiscoveryScope(
                    schema_name="dbo",
                    object_name="SalesHeader",
                    columns=("CompCode", "Type", "Status", "Date"),
                ),
            ),
        )

        @contextmanager
        def connection(self, **_kwargs):
            yield self

        def scalar(self, statement, params):
            sql = str(statement)
            if "HAS_PERMS" in sql:
                return int(params["table"] == "dbo.SalesFooter" or balances is not None)
            if "SELECT Unit" in sql:
                return unit
            assert "COUNT_BIG" in sql and params["product"] == "synthetic-product"
            return 0

        def execute(self, statement, params):
            sql = str(statement)
            if "FROM dbo.StockInventory" in sql:
                assert params == {"product": "synthetic-product", "company": "hila23"}
                return Rows(balances)
            if "MIN(Date)" in sql:
                return Rows(
                    [
                        dict(
                            first_date=date(2023, 1, 2),
                            last_date=date(2023, 2, 16),
                            calendar_days=46,
                        )
                    ]
                )
            assert "h.IDBranch=f.IDBranch" in sql
            assert params["product"] == "synthetic-product"
            assert "branch" not in params and "location" not in params
            return Rows(lines)

    return HilaleyihHistoricalReader(
        Connector(),
        SimpleNamespace(environment="development", hilaleyih_test_mode=True),
    )


def line(**changes):
    row = dict(
        Type="SI",
        InvNumber=1,
        Recno=0,
        CompCode="hila23",
        IDBranch="1",
        Qty=1,
        FreeQty=0,
        GrossTotal=100,
        PackUnit="pc",
        QtyInPack=1,
        PackUnitFree="pc",
        FQtyInPack=1,
        DocumentDate=date(2023, 1, 2),
    )
    return dict(row, **changes)


def test_positive_demand_excludes_unknown_negative_document_without_netting():
    reader = reader_for([line(Qty=Decimal("12.5")), line(Qty=-2, GrossTotal=-10)])
    evidence = reader.inventory("synthetic-product")
    assert evidence["line_count"] == 2 and evidence["usable_lines"] == 1
    assert evidence["negative_lines"] == 1
    assert evidence["demand_quantity"] == Decimal("12.5")
    assert demand_proxy(evidence["demand_quantity"], 46) == 2
    assert evidence["records"] == ["SI/1/0/hila23/1"]


@pytest.mark.parametrize(
    "changes",
    [
        {"Qty": None},
        {"FreeQty": None},
        {"GrossTotal": None},
        {"DocumentDate": None},
        {"PackUnit": "box"},
        {"QtyInPack": 12},
        {"FreeQty": 2, "PackUnitFree": "box"},
    ],
)
def test_unknown_quantities_or_pack_units_cannot_create_estimate(changes):
    evidence = reader_for([line(**changes)]).inventory("synthetic-product")
    assert evidence["usable_lines"] == 0
    assert "no evidence-based numerical estimate" in render_inventory(
        "Product", evidence
    )


def test_missing_product_unit_or_no_sales_does_not_invent_zero_stock():
    for reader in (reader_for([line()], unit=None), reader_for([])):
        evidence = reader.inventory("synthetic-product")
        reply = render_inventory("Product", evidence)
        assert "no evidence-based numerical estimate" in reply
        assert "does not mean zero stock" in reply


def test_matching_historical_balance_precedes_demand_proxy():
    evidence = reader_for(
        [], balances=[dict(WH="internal", Unit="pc", Balance=Decimal("4.5"))]
    ).inventory("synthetic-product")
    reply = render_inventory("Product", evidence)
    assert "historical source balance 4.5 pc" in reply
    assert "DEMO stock proxy" not in reply and "date is unknown" in reply


@pytest.mark.parametrize(
    "balances",
    [
        [dict(WH="internal", Unit="box", Balance=2)],
        [dict(WH="internal", Unit="pc", Balance=2)] * 2,
        [dict(WH="internal", Unit="pc", Balance=None)],
    ],
)
def test_ambiguous_or_unit_mismatched_balances_are_not_used(balances):
    evidence = reader_for([line(Qty=10)], balances=balances).inventory(
        "synthetic-product"
    )
    assert evidence["balance"] is None
    assert evidence["demand_quantity"] == 10
