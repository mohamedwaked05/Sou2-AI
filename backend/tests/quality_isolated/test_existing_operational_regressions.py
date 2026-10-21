"""Execute existing approval-flow regressions unchanged against revision 19."""

import pytest
from tests.operational_isolated import test_native_operations as baseline

native_source = baseline.native_source
test_real_approval_authorization_retrieval_and_audit = (
    baseline.test_real_approval_authorization_retrieval_and_audit
)


@pytest.fixture(scope="session")
def operations_platform(quality_platform):
    return quality_platform
