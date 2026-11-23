"""Real persistence checks for early replay rejection and safe provider diagnostics."""

from copy import deepcopy

import pytest

from app.core.exceptions import LLMOutputError, ReplayContractError
from app.db.models import GeneratedTest, RegressionSpecOperation
from app.schemas.attributions import ProviderUsage
from app.schemas.llm_diagnostics import OutputDiagnostics, OutputValidationIssue
from tests.integration.test_attributions import attribution_setup as attribution_setup
from tests.integration.test_attributions import submit as submit_attribution
from tests.integration.test_generated_tests import compilation_setup as compilation_setup
from tests.integration.test_regression_specs import setup_regression as setup_regression
from tests.integration.test_regression_specs import submit as submit_regression

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("stage", ["attribution", "regression"])
def test_rejected_output_diagnostics_survive_terminal_delivery_without_second_call(
    stage, request, monkeypatch
):
    setup = request.getfixturevalue(
        "attribution_setup" if stage == "attribution" else "setup_regression"
    )
    if stage == "attribution":
        _, _, store, _, fake, service = setup
        accepted = submit_attribution(setup)
        method, schema, prompt = "attribute", "AttributionDecision", "attribution-v1"
    else:
        _, store, _, fake, service = setup
        accepted = submit_regression(setup)
        method, schema, prompt = "generate", "RegressionSpec", "regression-spec-v1"
    diagnostics = OutputDiagnostics(
        schema_name=schema,
        schema_sha256="a" * 64,
        prompt_version=prompt,
        response_sha256="b" * 64,
        category="schema_validation",
        error_count=1,
        issues=(OutputValidationIssue(category="missing", path=("evidence",)),),
        omitted_issue_count=0,
    )
    calls = []

    def rejected(request):
        calls.append(request)
        raise LLMOutputError(
            usage=ProviderUsage(response_id="resp_rejected", input_tokens=12, latency_ms=4),
            diagnostics=diagnostics,
        )

    monkeypatch.setattr(fake, method, rejected)
    service.process(accepted.id)
    service.process(accepted.id)
    result = store.get(accepted.id)
    assert result.status == "failed" and result.failure_code == "LLM_OUTPUT_INVALID"
    assert result.output_diagnostics == diagnostics and result.response_id == "resp_rejected"
    assert result.input_tokens == 12 and len(calls) == 1


def test_provenance_valid_nested_scalar_is_rejected_before_compiler(compilation_setup, monkeypatch):
    parent_id, _, service = compilation_setup
    with service.sessions.begin() as session:
        parent = session.get(RegressionSpecOperation, parent_id)
        raw = deepcopy(parent.spec)
        raw["scenario"]["preconditions"][0]["path"] = ["operation"]
        parent.spec = raw
        parent.scenario_snapshot = {
            "stimulus": {"left": 2, "right": 2},
            "preconditions": ["add"],
        }

    def forbidden(*args):
        raise AssertionError("Incompatible replay must not reach the compiler")

    monkeypatch.setattr(service.compiler, "compile", forbidden)
    with pytest.raises(ReplayContractError) as caught:
        service.compile(parent_id, "early-replay-check")
    artifact = service.get(caught.value.generated_test_id)
    assert artifact.status == "failed" and artifact.failure_code == "REPLAY_CONTRACT_INCOMPATIBLE"
    assert artifact.source_code is None and artifact.source_digest is None
    # Repeated requests reuse the durable failed attempt instead of repairing or retrying it.
    assert service.compile(parent_id, "repeat") == (artifact, False)
    with service.sessions() as session:
        assert session.get(GeneratedTest, artifact.id).failure_code == artifact.failure_code
