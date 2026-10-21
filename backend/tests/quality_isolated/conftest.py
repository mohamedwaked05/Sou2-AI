"""Identity-verified disposable PostgreSQL only; no destructive parent fixtures."""

import pytest
from alembic import command
from tests.isolated.conftest import platform as platform
from tests.isolated.conftest import tenant as tenant
from tests.operational_isolated.conftest import (
    operations_platform as operations_platform,
)


@pytest.fixture(scope="session")
def quality_platform(platform):
    command.upgrade(platform["config"], "20261015_18")
    command.upgrade(platform["config"], "20261018_19")
    return platform
