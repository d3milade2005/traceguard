"""Track B coordinates existing pure phase boundaries, with no oracle in inference."""

import time
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from app.core.exceptions import ApplicationError, ReplayContractError
from app.integrations.execution.contracts import ExecutionBundle
from app.integrations.llm.contracts import LLMClient
from app.integrations.llm.regression import RegressionGenerator
from app.schemas.attributions import LLMResult, ReasoningEffort
from app.schemas.regression_specs import RegressionResult
from app.schemas.validations import ExecutionResult, classify
from app.services.attribution_input import build_attribution_request
from app.services.attribution_validation import validate_attribution
from app.services.context import ContextManager
from app.services.pytest_compiler import PytestCompiler
from app.services.regression_input import (
    build_regression_request,
    load_evidence,
    preflight,
    review_spec,
)
from app.services.regression_validation import validate_regression
from evaluation.artifacts import (
    Experiment,
    RecordedCallFailure,
    digest,
    ensure_snapshot,
    read_json,
    safe_failure,
)
from evaluation.context import analysis_context, context_statistics
from evaluation.harness import validate_invocation
from evaluation.records import attribution_record, regression_record
from evaluation.scenarios import ControlledScenario


class SuiteValidator(Protocol):
    def validate(
        self, bundle: ExecutionBundle, validation_id: UUID
    ) -> tuple[ExecutionResult, ExecutionResult]: ...
    def identity(self) -> dict: ...


def evaluate_scenario(
    scenario: ControlledScenario,
    manager: ContextManager,
    client: LLMClient,
    generator: RegressionGenerator,
    validator: SuiteValidator,
    experiment: Experiment,
    *,
    model: str,
    effort: ReasoningEffort,
    max_calls: int,
) -> dict:
    started = time.monotonic()
    row = {
        "attribution_success": False,
        "synthesizable": False,
        "needs_review": False,
        "compiled": False,
        "replay_compatible": False,
        "abstained": False,
        "verdict": None,
        "end_to_end_success": False,
        "failure": None,
        "usages": [],
        "prediction": None,
        "spec": None,
    }
    stage = "execution"
    prediction = None
    attribution_valid = False
    try:
        trace = scenario.execute("faulty")
        row["trace_digest"] = digest(trace.model_dump(mode="json"))
        stage = "context"
        context = analysis_context(manager, trace)
        row["context"] = context_statistics(context)
        stage = "attribution_input"
        request = build_attribution_request(context, trace, model=model, reasoning_effort=effort)
        stage = "attribution_provider"
        result = experiment.call(
            scenario.sample_id,
            "attribution",
            request,
            LLMResult,
            lambda: client.attribute(request),
            max_calls=max_calls,
        )
        prediction = result.attribution
        row["prediction"] = prediction.model_dump(mode="json")
        row["usages"].append(result.usage.model_dump(mode="json"))
        stage = "attribution_validation"
        validate_attribution(prediction, context, trace)
        attribution_valid = True
        attribution = attribution_record(
            scenario.sample_id, result, context, model=model, effort=effort
        )
        row["abstained"] = prediction.decision == "abstain"
        if not row["abstained"]:
            stage = "regression_input"
            evidence = load_evidence(attribution, trace)
            reason = preflight(evidence)
            if reason is not None:
                regression = RegressionResult(spec=review_spec(attribution, reason))
            else:
                request_spec = build_regression_request(
                    evidence, model=model, reasoning_effort=effort, max_input_bytes=48000
                )
                stage = "regression_provider"
                regression = experiment.call(
                    scenario.sample_id,
                    "regression",
                    request_spec,
                    RegressionResult,
                    lambda: generator.generate(request_spec),
                    max_calls=max_calls,
                )
                row["usages"].append(regression.usage.model_dump(mode="json"))
            stage = "regression_validation"
            snapshot = validate_regression(regression.spec, evidence, max_scenario_bytes=16000)
            row["spec"] = regression.spec.model_dump(mode="json")
            row["needs_review"] = regression.spec.disposition == "needs_review"
            row["synthesizable"] = snapshot is not None
            if snapshot is not None:
                stage = "replay_contract"
                try:
                    if (
                        regression.spec.scenario is None
                        or regression.spec.scenario.stimulus.field != "input"
                    ):
                        raise ValueError("Unsupported replay stimulus")
                    validate_invocation(
                        target_agent=regression.spec.target_agent or "",
                        stimulus=snapshot.stimulus,
                        preconditions=snapshot.preconditions,
                    )
                except ValueError as exc:
                    raise ReplayContractError from exc
                row["replay_compatible"] = True
                stage = "compilation"
                record = regression_record(
                    scenario.sample_id, regression, snapshot, model=model, effort=effort
                )
                compiled = PytestCompiler(
                    max_source_bytes=262144, max_scenario_bytes=16000
                ).compile(record)
                artifact = {
                    "source_code": compiled.source_code,
                    "source_digest": compiled.source_digest,
                    "compiler_version": compiled.compiler_version,
                    "test_name": compiled.test_name,
                    "spec": regression.spec.model_dump(mode="json"),
                    "scenario_snapshot": snapshot.model_dump(mode="json"),
                }
                ensure_snapshot(
                    experiment.root / "artifacts" / f"{digest(scenario.sample_id)}.json", artifact
                )
                row["compiled"] = True
                row["artifact_digest"] = compiled.source_digest
                stage = "sandbox"
                validation_path = (
                    experiment.root / "validation" / f"{digest(scenario.sample_id)}.json"
                )
                identity = validator.identity()
                if validation_path.exists():
                    validation = read_json(validation_path)
                    if (
                        validation["runtime"] != identity
                        or validation["source_digest"] != compiled.source_digest
                    ):
                        raise ValueError("Sandbox runtime or source changed while resuming")
                else:
                    faulty, corrected = validator.validate(
                        ExecutionBundle(compiled.source_code, compiled.test_name),
                        uuid5(NAMESPACE_URL, experiment.id + scenario.sample_id),
                    )
                    validation = {
                        "source_digest": compiled.source_digest,
                        "runtime": identity,
                        "faulty": faulty.model_dump(mode="json"),
                        "corrected": corrected.model_dump(mode="json"),
                        "verdict": classify(faulty, corrected),
                    }
                    ensure_snapshot(validation_path, validation)
                row["validation"] = validation
                row["verdict"] = validation["verdict"]
    except RecordedCallFailure as exc:
        row["failure"] = exc.failure | {"stage": stage}
        if exc.failure.get("usage"):
            row["usages"].append(exc.failure["usage"])
    except ApplicationError as exc:
        row["failure"] = safe_failure(exc) | {"stage": stage}
    # Private oracle is used only for scoring, after all inference stages terminate.
    row["ground_truth"] = scenario.ground_truth()
    row["attribution_success"] = (
        prediction is not None
        and attribution_valid
        and (
            prediction.decision == "attributed"
            and prediction.responsible_agent == scenario.oracle.responsible_agent
            and prediction.decisive_sequence == scenario.oracle.decisive_sequence
        )
    )
    row["end_to_end_success"] = (
        row["attribution_success"]
        and row["synthesizable"]
        and row["compiled"]
        and (row["verdict"] == "valid")
    )
    row["wall_latency_ms"] = round((time.monotonic() - started) * 1000)
    return row
