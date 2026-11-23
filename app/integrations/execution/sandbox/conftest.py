"""Pinned PyTest fixture and assertion provenance hook, used only in the sandbox."""

import json
import os
from collections.abc import Generator
from pathlib import Path

import pytest
from pluggy import Result
from pydantic import JsonValue

from app.integrations.execution.calculator import CorrectedCalculator, FaultyCalculator
from app.integrations.execution.langgraph_refund import CONTRACT, replay
from app.integrations.execution.refund import DECISION_AGENT, RefundEligibilityHarness
from app.regression_runtime import RegressionObservation, RegressionTestHarness


class ControlledHarness:
    identifier = "traceguard.agent-invocation"
    version = "1"

    def __init__(self, variant: str) -> None:
        self.variant = variant

    def invoke(
        self, *, target_agent: str, stimulus: JsonValue, preconditions: tuple[JsonValue, ...]
    ) -> RegressionObservation:
        harness: RegressionTestHarness
        if isinstance(stimulus, dict) and stimulus.get("replay_contract") == CONTRACT:
            fixtures = json.loads(os.environ["TRACEGUARD_REPLAY_FIXTURES"])
            return replay(target_agent, stimulus, preconditions, fixtures, self.variant)
        if target_agent == DECISION_AGENT:
            harness = RefundEligibilityHarness(self.variant)
        else:
            harness = FaultyCalculator() if self.variant == "faulty" else CorrectedCalculator()
        return harness.invoke(
            target_agent=target_agent, stimulus=stimulus, preconditions=preconditions
        )


@pytest.fixture
def regression_harness() -> RegressionTestHarness:
    variant = os.environ["TRACEGUARD_VARIANT"]
    if variant == "faulty":
        return ControlledHarness(variant)
    if variant == "corrected":
        return ControlledHarness(variant)
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
