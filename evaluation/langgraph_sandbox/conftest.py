"""Research graph fixture; retain the production assertion-provenance hook."""

import os

import pytest

from app.integrations.execution.sandbox.conftest import pytest_runtest_makereport  # noqa: F401
from app.regression_runtime import RegressionTestHarness
from evaluation.langgraph_sandbox.harness import LangGraphRefundHarness


@pytest.fixture
def regression_harness() -> RegressionTestHarness:
    return LangGraphRefundHarness(os.environ["TRACEGUARD_VARIANT"])
