"""Evaluation-only fixture; assertion provenance comes from the production hook."""

import os

import pytest

from app.integrations.execution.sandbox.conftest import pytest_runtest_makereport  # noqa: F401
from app.regression_runtime import RegressionTestHarness
from evaluation.harness import SuiteHarness


@pytest.fixture
def regression_harness() -> RegressionTestHarness:
    variant = os.environ["TRACEGUARD_VARIANT"]
    if variant not in {"faulty", "corrected"}:
        raise ValueError("Unsupported evaluation variant")
    return SuiteHarness(faulty=variant == "faulty")
