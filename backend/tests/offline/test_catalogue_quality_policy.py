"""Exact policy arithmetic and deterministic Owner Chat coverage disclosure."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.core.config import Settings
from app.integrations.catalogue_quality import within_tolerance
from app.schemas.source_mapping import CatalogueCoverage
from app.services.owner_chat import _catalogue_reply
from pydantic import ValidationError
from test_catalogue_chat import catalogue, product


def test_exact_arithmetic_does_not_round_threshold_comparisons():
    assert within_tolerance(10, 1000, Decimal("1"))
    assert not within_tolerance(11, 1000, Decimal("1"))
    assert not within_tolerance(1, 300, Decimal("0.3333"))
    for value in ("NaN", "Infinity", "5.0001", "100", "-1", "0.00001"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, catalogue_max_invalid_percent=value)


@pytest.mark.parametrize("empty", [True, False])
def test_partial_catalogue_disclosure_is_concise_and_source_backed(empty):
    output = catalogue(*(() if empty else (product(),)))
    output = output.model_copy(
        update={
            "coverage": CatalogueCoverage(
                approval_type="partial",
                excluded_records=10,
                total_records=9353,
                valid_records=9343,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        }
    )
    reply = _catalogue_reply(output, "List matching products")
    assert "10 invalid source records" not in reply
    assert output.coverage.excluded_records == 10
    assert "Stock and prices are unknown" in reply
