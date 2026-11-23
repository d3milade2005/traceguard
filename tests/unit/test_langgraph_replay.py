"""Contract and graph checks; no generated test is executed on the host."""

from copy import deepcopy
from uuid import uuid4

import pytest

from app.core.exceptions import SandboxPolicyError
from evaluation.langgraph_sandbox.harness import (
    STIMULUS,
    TARGET,
    LangGraphRefundHarness,
    validate_invocation,
)


@pytest.mark.parametrize("variant,approved", [("faulty", True), ("corrected", False)])
def test_replay_executes_actual_fixed_graph(variant, approved):
    pytest.importorskip("langgraph")
    observation = LangGraphRefundHarness(variant).invoke(
        target_agent=TARGET, stimulus=STIMULUS, preconditions=()
    )
    assert observation.output == {"approved": approved}
    assert observation.status == "success"


@pytest.mark.parametrize("value", [None, {}, [], True, 21, {"intake": {}}])
def test_unknown_stimulus_is_rejected(value):
    with pytest.raises(ValueError):
        validate_invocation(TARGET, value, ())


def test_boolean_integer_substitution_is_rejected():
    stimulus = deepcopy(STIMULUS)
    stimulus["policy"]["outside_window"]["approved"] = 0
    with pytest.raises(ValueError):
        validate_invocation(TARGET, stimulus, ())


def test_changed_dates_setup_target_and_variant_are_rejected():
    stimulus = deepcopy(STIMULUS)
    stimulus["intake"]["request_date"] = "2026-09-23"
    for target, value, setup in (
        (TARGET, stimulus, ()),
        ("arbitrary", STIMULUS, ()),
        (TARGET, STIMULUS, ({"mode": "public"},)),
    ):
        with pytest.raises(ValueError):
            validate_invocation(target, value, setup)
    with pytest.raises(ValueError):
        LangGraphRefundHarness("anything")


def test_overlay_keeps_containment_and_rejects_wrong_runtime():
    from app.schemas.validations import RuntimeIdentity, SandboxLimits
    from evaluation.langgraph_replay import LangGraphDockerRunner
    from scripts.evaluate_regressions import sandbox_settings

    # Test create policy without connecting to Docker.
    runner = object.__new__(LangGraphDockerRunner)
    runner.settings = sandbox_settings()
    runner.base_runtime = RuntimeIdentity(
        image_reference="example:1",
        image_id="sha256:" + "1" * 64,
        bundle_digest="2" * 64,
        limits=SandboxLimits.from_settings(runner.settings),
    )
    runner.replay_image_id = "sha256:research-overlay"
    args = runner.create_args(runner.base_runtime, "faulty", uuid4(), uuid4(), "example")
    assert args[-1] == "sha256:research-overlay"
    assert args[args.index("--network") + 1] == "none"
    assert "--read-only" in args
    assert "seccomp=builtin" in args
    assert "10001:10001" in args
    assert not any(arg in {"--volume", "-v", "--privileged"} for arg in args)
    with pytest.raises(SandboxPolicyError):
        runner.create_args(
            runner.base_runtime.model_copy(update={"image_id": "sha256:" + "3" * 64}),
            "faulty",
            uuid4(),
            uuid4(),
            "example",
        )
