"""Pinned PyTest fixture and assertion provenance hook, used only in the sandbox."""

import os
from collections.abc import Generator
from pathlib import Path

import pytest
from pluggy import Result

from app.integrations.execution.calculator import CorrectedCalculator, FaultyCalculator
from app.regression_runtime import RegressionTestHarness


@pytest.fixture
def regression_harness() -> RegressionTestHarness:
    variant = os.environ["TRACEGUARD_VARIANT"]
    if variant == "faulty":
        return FaultyCalculator()
    if variant == "corrected":
        return CorrectedCalculator()
    raise ValueError("Unsupported trusted variant")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, Result[pytest.TestReport], None]:
    outcome = yield
    report = outcome.get_result()
    expected = False
    if call.when == "call" and call.excinfo is not None and call.excinfo.type is AssertionError:
        frame = call.excinfo.traceback[-1]
        expected = (
            Path(str(frame.path)) == Path("/opt/runtime/app/regression_runtime.py")
            and frame.name == "_assert"
        )
    if call.when == "call":
        item.user_properties.append(("traceguard_assertion", "true" if expected else "false"))
    report.user_properties = list(item.user_properties)
