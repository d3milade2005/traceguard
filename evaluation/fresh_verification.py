"""Pre-freeze tests with explicit fake responses; these are not research observations."""

from pathlib import Path

from app.integrations.execution.langgraph_refund import DECISION, INTAKE
from app.schemas.attributions import AttributionDecision, EvidenceReference, LLMResult
from app.schemas.regression_specs import (
    Assertion,
    Citation,
    RegressionResult,
    RegressionSpec,
    Scenario,
    SourceReference,
)
from evaluation.fresh_provider import write
from examples.langgraph_refund.fresh_application import SCENARIOS, execute
from tests.unit.test_fresh_e2e import FakeProvider


class OfflineProvider(FakeProvider):
    def __init__(self, target, index, trace):
        super().__init__()
        self.target, self.index, self.trace = target, index, trace
        self.attribution_calls = self.specification_calls = 0

    def attribute(self, request):
        self.attribution_calls += 1
        return LLMResult(
            attribution=AttributionDecision(
                decision="attributed",
                responsible_agent=self.target,
                decisive_sequence=self.index,
                failure_category="application_logic",
                explanation="Offline fixed fixture diagnosis.",
                confidence="high",
                evidence=(
                    EvidenceReference(
                        start_sequence=0, end_sequence=4, reason="Complete synthetic trace"
                    ),
                ),
                abstention_reason=None,
            )
        )

    def generate(self, request):
        self.specification_calls += 1
        import json

        event = self.trace["events"][self.index]
        if self.target == INTAKE:
            path, value, field = "days_since_purchase", 21, "output"
        elif self.target == DECISION:
            path, value, field = "approved", False, "output"
        else:
            path, value, field = "communicated_approved", False, "input"
        fault = Citation(
            sequence=self.index,
            field="output",
            quote=json.dumps(event["output"], sort_keys=True, separators=(",", ":")),
        )
        expected = Citation(
            sequence=self.index,
            field=field,
            quote=json.dumps(event[field], sort_keys=True, separators=(",", ":")),
        )
        spec = RegressionSpec(
            version="regression-spec-v1",
            disposition="synthesizable",
            test_name="test_offline_contract",
            title="Offline compiler and sandbox contract check",
            target_agent=self.target,
            failure_summary="Synthetic fixture violates the declared invariant.",
            scenario=Scenario(
                stimulus=SourceReference(sequence=self.index, field="input", path=()),
                preconditions=(),
            ),
            assertions=(
                Assertion(
                    kind="equals",
                    observable="output",
                    path=(path,),
                    expected=value,
                    evidence=(fault, expected),
                ),
            ),
            evidence=(fault,),
            review_reason=None,
        )
        return RegressionResult(spec=spec)


def verify(root: Path, settings, *, use_services: bool):
    from scripts.run_fresh_langgraph import database, export_database, pipeline

    if not use_services:
        raise ValueError("Use offline-services to verify the entire actual service path")
    root = root / f"offline-services-{len(list(root.glob('offline-services-*'))) + 1}"
    root.mkdir(exist_ok=False)
    engine, sessions = database(root)
    try:
        for case, target, index in SCENARIOS:
            path = root / "cases" / case
            path.mkdir(parents=True)
            fake = FakeProvider()
            trace = execute(target, fake)
            provider = OfflineProvider(target, index, trace)
            provider.fixtures = fake.fixtures
            result = pipeline(root, sessions, settings, case, target, provider, trace["envelope"])
            write(
                path / "fake-call-counts.json",
                {
                    "attribution": provider.attribution_calls,
                    "specification": provider.specification_calls,
                },
            )
            assert provider.attribution_calls == provider.specification_calls == 1
            assert result["analysis"]["status"] == "completed", result
            assert result["validation"]["verdict"] == "valid", result
            assert len(result["additional_pairs"]) == 2
            assert all(
                p["faulty"]["outcome"] == "assertion_failed"
                and p["corrected"]["outcome"] == "passed"
                for p in result["additional_pairs"]
            )
            print(case, "offline actual-services + three Docker pairs valid", flush=True)
        export_database(sessions, root)
    finally:
        engine.dispose()
