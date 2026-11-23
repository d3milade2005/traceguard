import ast
import hashlib

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import (
    CompilationError,
    GeneratedTestNotFoundError,
    ValidationArtifactError,
)
from app.db.models.generated_test import GeneratedTest
from app.db.models.regression_spec import RegressionSpecOperation
from app.integrations.execution.contracts import ExecutionBundle
from app.integrations.execution.registry import approved_harness
from app.schemas.generated_tests import GeneratedTestRecord
from app.schemas.regression_specs import RegressionRecord
from app.services.pytest_compiler import (
    ASSERTION_HELPERS,
    COMPILER_VERSION,
    canonical_json,
    validate_compilation_input,
    validate_source,
)

# Trusted fixed scaffolding, parsed statically. No source is regenerated or replaced.
SCAFFOLD = """import json
from app import regression_runtime as runtime
def test_placeholder(regression_harness):
    scenario = json.loads("placeholder")
    harness = runtime.require_harness(regression_harness, 'traceguard.agent-invocation', '1')
    observation = runtime.require_observation(
        harness.invoke(
            target_agent='placeholder', stimulus=scenario["stimulus"],
            preconditions=tuple(scenario["preconditions"])
        )
    )
"""


def verify_artifact(
    session: Session, row: GeneratedTest | None, settings: Settings
) -> tuple[GeneratedTestRecord, ExecutionBundle]:
    if row is None:
        raise GeneratedTestNotFoundError
    try:
        artifact = GeneratedTestRecord.model_validate(row)
        if artifact.status != "completed" or artifact.compiler_version != COMPILER_VERSION:
            raise ValidationArtifactError
        harness = approved_harness(artifact.harness_identifier, artifact.harness_version)
        if (
            not artifact.source_code
            or not artifact.test_name
            or hashlib.sha256(artifact.source_code.encode("utf-8")).hexdigest()
            != artifact.source_digest
        ):
            raise ValidationArtifactError
        parent = session.get(RegressionSpecOperation, artifact.regression_spec_id)
        if parent is None:
            raise ValidationArtifactError
        record = RegressionRecord.model_validate(parent)
        spec, snapshot = validate_compilation_input(
            record, max_scenario_bytes=settings.regression_max_scenario_bytes
        )
        harness.check(spec, snapshot)
        validate_source(
            artifact.source_code,
            expected_source=artifact.source_code,
            test_name=spec.test_name,
            max_source_bytes=settings.generated_test_max_source_bytes,
        )
        # Populate only AST literal nodes and approved helpers; never emit or execute Python.
        expected = ast.parse(SCAFFOLD)
        function = expected.body[2]
        assert isinstance(function, ast.FunctionDef)
        function.name = spec.test_name
        fixture = function.body[0]
        assert isinstance(fixture, ast.Assign) and isinstance(fixture.value, ast.Call)
        fixture.value.args[0] = ast.Constant(canonical_json(snapshot.model_dump(mode="json")))
        observation = function.body[2]
        assert isinstance(observation, ast.Assign) and isinstance(observation.value, ast.Call)
        invocation = observation.value.args[0]
        assert isinstance(invocation, ast.Call)
        invocation.keywords[0].value = ast.Constant(spec.target_agent)
        for assertion in spec.assertions:
            args: list[ast.expr] = [
                ast.Attribute(
                    ast.Name("observation", ast.Load()), assertion.observable, ast.Load()
                ),
                ast.Tuple([ast.Constant(part) for part in assertion.path], ast.Load()),
            ]
            if assertion.kind not in {"exists", "not_exists"}:
                args.append(
                    ast.Call(
                        ast.Attribute(ast.Name("json", ast.Load()), "loads", ast.Load()),
                        [ast.Constant(canonical_json(assertion.expected))],
                        [],
                    )
                )
            function.body.append(
                ast.Expr(
                    ast.Call(
                        ast.Attribute(
                            ast.Name("runtime", ast.Load()),
                            ASSERTION_HELPERS[assertion.kind],
                            ast.Load(),
                        ),
                        args,
                        [],
                    )
                )
            )
        if artifact.test_name != spec.test_name or ast.dump(expected) != ast.dump(
            ast.parse(artifact.source_code)
        ):
            raise ValidationArtifactError
        return artifact, ExecutionBundle(artifact.source_code, artifact.test_name)
    except (ValidationError, CompilationError, ValueError, TypeError, AttributeError) as exc:
        raise ValidationArtifactError from exc
