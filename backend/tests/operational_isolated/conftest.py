"""Reuse identity-verified disposable PostgreSQL bootstrap, never parent fixtures."""

import pytest
from alembic import command
from sqlalchemy import text
from tests.isolated.conftest import platform as platform
from tests.isolated.conftest import tenant as tenant


@pytest.fixture(scope="session")
def operations_platform(platform):
    # Existing users table belongs to the bootstrap table owner, not migrator.
    # Use that verified owner for new FK creation; never runtime credentials.
    command.upgrade(platform["config"], "20261015_18")
    with platform["admin"].connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.source_operational_mapping_revisions")
            )
            == 0
        )
    return platform
