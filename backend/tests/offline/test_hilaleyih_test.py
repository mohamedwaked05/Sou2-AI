"""Test-mode isolation, stable demo values and truthful financial disclosures."""

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.core.config import Settings
from app.integrations.discovery import SourceMappingError
from app.integrations.hilaleyih_test import (
    BUSINESS_ID,
    HilaleyihHistoricalReader,
    demand_proxy,
    estimated_price,
)
from app.services.hilaleyih_test import (
    enabled,
    month_bounds,
    product_query,
    render_financial,
)
from pydantic import SecretStr


@pytest.mark.parametrize("environment", ["production", "testing", "staging"])
def test_only_development_can_enable_exceptions(environment):
    settings = SimpleNamespace(environment=environment, hilaleyih_test_mode=True)
    assert not enabled(settings, uuid.UUID(BUSINESS_ID))


def test_exact_tenant_and_explicit_switch_are_required():
    settings = SimpleNamespace(environment="development", hilaleyih_test_mode=True)
    assert enabled(settings, uuid.UUID(BUSINESS_ID))
    assert not enabled(settings, uuid.uuid4())
    settings.hilaleyih_test_mode = False
    assert not enabled(settings, uuid.UUID(BUSINESS_ID))


def test_prices_cover_catalogue_and_stock_requires_positive_quantity_evidence():
    assert all(Decimal("0.50") <= estimated_price(str(i)) <= 20 for i in range(9343))
    assert demand_proxy(Decimal(288), 46) == 44
    assert demand_proxy(Decimal(0), 46) is None
    assert demand_proxy(Decimal(288), 0) is None
    assert demand_proxy(Decimal(-2), 46) is None


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What is the price of Pepsi Bottle 1.5 L?", "pepsi 1.5 l"),
        ("How much does this product cost?", ""),
        ("How many Pepsi bottles are in stock?", "pepsi"),
        ("Check stock in the Jbeil branch.", ""),
    ],
)
def test_product_reference_preserves_sizes_and_drops_question_words(question, expected):
    assert product_query(question) == expected


def test_calendar_period_is_month_exclusive_and_supports_leap_years():
    assert month_bounds("revenue February 2023") == (date(2023, 2, 1), date(2023, 3, 1))
    assert month_bounds("revenue Feb 2024") == (date(2024, 2, 1), date(2024, 3, 1))
    assert month_bounds("December 2023") == (date(2023, 12, 1), date(2024, 1, 1))
    assert month_bounds("this month") is None


@pytest.mark.parametrize("settlement", [None, 3])
def test_provisional_finance_does_not_classify_unknown_refunds_or_hide_discrepancies(
    settlement,
):
    row = dict(
        currency_code="source-code",
        documents=5,
        gross=100,
        discounts=10,
        taxes=2,
        signed_total=92,
        negative_documents=1,
        negative_total=-5,
        incomplete_documents=0,
        settlement_discrepancies=settlement,
        settlement_difference=Decimal("0.37") if settlement else None,
    )
    reply = render_financial([row], date(2023, 2, 1), date(2023, 3, 1))
    assert "= 92" in reply and "negative documents: 1" in reply
    assert "classification is unknown" in reply
    assert "not audited or approved" in reply
    assert ("SQL Server denies" if settlement is None else "0.37") in reply


def test_reader_cannot_target_a_production_database():
    settings = Settings(_env_file=None).model_copy(
        update={
            "environment": "development",
            "hilaleyih_test_mode": True,
        }
    )
    connector = SimpleNamespace(
        config=SimpleNamespace(
            url=SecretStr("mssql+pyodbc://localhost/production"),
        )
    )
    with pytest.raises(SourceMappingError, match="hilaleyih_test_database_required"):
        HilaleyihHistoricalReader(connector, settings)
    with pytest.raises(SourceMappingError, match="hilaleyih_test_disabled"):
        HilaleyihHistoricalReader(
            connector, settings.model_copy(update={"environment": "production"})
        )
