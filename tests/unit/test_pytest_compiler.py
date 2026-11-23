import ast
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.exceptions import (
    CompilationInputError,
    GeneratedSafetyError,
    GeneratedSyntaxError,
    RegressionNotSynthesizableError,
    UnsupportedSpecVersionError,
)
from app.schemas.regression_specs import ScenarioSnapshot
from app.services.pytest_compiler import ASSERTION_HELPERS, PytestCompiler, validate_source
from tests.fixtures.replayable import calculator_record


@pytest.fixture
def compiler():
    return PytestCompiler(max_source_bytes=262144, max_scenario_bytes=16000)


def test_realistic_record_compiles_to_one_predictable_test(compiler):
    artifact = compiler.compile(calculator_record())
    tree = ast.parse(artifact.source_code)
    assert len(tree.body) == 3
    assert isinstance(tree.body[0], ast.Import) and tree.body[0].names[0].name == "json"
    assert isinstance(tree.body[1], ast.ImportFrom) and tree.body[1].module == "app"
    function = tree.body[2]
    assert isinstance(function, ast.FunctionDef) and function.name == artifact.test_name
    assert [arg.arg for arg in function.args.args] == ["regression_harness"]
    fixture_call = function.body[0].value
    assert isinstance(fixture_call, ast.Call) and isinstance(fixture_call.args[0], ast.Constant)
    fixture = json.loads(fixture_call.args[0].value)
    assert fixture == {"stimulus": {"left": 2, "right": 2}, "preconditions": [{"operation": "add"}]}
    assert fixture != {"stimulus": {"sum": 5}}
    assert artifact.source_digest == hashlib.sha256(artifact.source_code.encode()).hexdigest()
    assert artifact.framework == "pytest" and artifact.compiler_version == "pytest-compiler-v1"
    assert (
        "runtime.assert_equals(observation.output, ('sum',), json.loads('4'))"
        in artifact.source_code
    )


@pytest.mark.parametrize("kind", list(ASSERTION_HELPERS))
def test_each_assertion_maps_to_its_fixed_helper(compiler, kind):
    record = calculator_record()
    assertion = record.spec.assertions[0].model_copy(
        update={"kind": kind, "expected": None if kind in {"exists", "not_exists"} else "4"}
    )
    ir = record.spec.model_copy(update={"assertions": (assertion,)})
    source = compiler.compile(record.model_copy(update={"spec": ir})).source_code
    assert f"runtime.{ASSERTION_HELPERS[kind]}(observation.output, ('sum',)" in source
    ast.parse(source)


def test_multiple_assertions_and_all_observables(compiler):
    record = calculator_record()
    assertions = tuple(
        record.spec.assertions[0].model_copy(update={"observable": observable})
        for observable in ("output", "tool_result", "status")
    )
    artifact = compiler.compile(
        record.model_copy(
            update={"spec": record.spec.model_copy(update={"assertions": assertions})}
        )
    )
    for observable in ("output", "tool_result", "status"):
        assert f"runtime.assert_equals(observation.{observable}," in artifact.source_code


def test_source_and_digest_ignore_identity_times_prose_and_dictionary_order(compiler):
    record = calculator_record()
    first = compiler.compile(record)
    changed = record.model_copy(
        update={
            "id": uuid4(),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "scenario_snapshot": record.scenario_snapshot.model_copy(
                update={"stimulus": {"right": 2, "left": 2}}
            ),
            "spec": record.spec.model_copy(
                update={"failure_summary": "A different prose explanation."}
            ),
        }
    )
    second = compiler.compile(changed)
    assert first.source_code == second.source_code and first.source_digest == second.source_digest
    assert first == compiler.compile(record)


def test_safe_literal_escaping_preserves_unicode_quotes_and_newlines(compiler):
    record = calculator_record()
    text = "quotes '\" and backslashes \\ and\nUnicode: λ"
    snapshot = record.scenario_snapshot.model_copy(
        update={"stimulus": {"left": 2, "right": 2, "text": text}}
    )
    source = compiler.compile(record.model_copy(update={"scenario_snapshot": snapshot})).source_code
    node = ast.parse(source).body[2].body[0].value.args[0]
    assert json.loads(node.value)["stimulus"]["text"] == text
    assert source.isascii() and source.endswith("\n") and "\r" not in source


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"status": "pending"}, RegressionNotSynthesizableError),
        ({"status": "failed"}, RegressionNotSynthesizableError),
        ({"disposition": "needs_review"}, RegressionNotSynthesizableError),
        ({"spec": None}, CompilationInputError),
        ({"scenario_snapshot": None}, CompilationInputError),
        ({"version": "regression-spec-v999"}, UnsupportedSpecVersionError),
        ({"target_agent": "other"}, CompilationInputError),
        ({"review_reason": "uncertain"}, CompilationInputError),
    ],
)
def test_ineligible_and_inconsistent_record_rejected(compiler, changes, error):
    with pytest.raises(error):
        compiler.compile(calculator_record().model_copy(update=changes))


def test_spec_revalidation_rejects_unsupported_assertions_versions_and_paths(compiler):
    record = calculator_record()
    with pytest.raises(UnsupportedSpecVersionError):
        compiler.compile(
            record.model_copy(update={"spec": record.spec.model_copy(update={"version": "other"})})
        )
    for changes in ({"kind": "python"}, {"path": ("sum.real",)}, {"path": (True,)}):
        bad = record.spec.model_copy(
            update={"assertions": (record.spec.assertions[0].model_copy(update=changes),)}
        )
        with pytest.raises(CompilationInputError):
            compiler.compile(record.model_copy(update={"spec": bad}))


def test_unvalidated_extra_contract_fields_are_not_silently_discarded(compiler):
    record = calculator_record()
    for changes in (
        {"spec": record.spec.model_copy(update={"entry_point": "arbitrary.module"})},
        {"scenario_snapshot": record.scenario_snapshot.model_copy(update={"callback": "anything"})},
    ):
        with pytest.raises(CompilationInputError):
            compiler.compile(record.model_copy(update=changes))


@pytest.mark.parametrize(
    "payload", ["eval('x')", "import os", "rm -rf /", "def attack(): pass", "subprocess.run('x')"]
)
def test_code_like_scenario_data_is_rejected_without_interpretation(compiler, payload):
    record = calculator_record()
    snapshot = record.scenario_snapshot.model_copy(
        update={"stimulus": {"left": 2, "payload": payload}}
    )
    with pytest.raises(CompilationInputError):
        compiler.compile(record.model_copy(update={"scenario_snapshot": snapshot}))


def test_invalid_snapshot_and_finite_numeric_bounds(compiler):
    record = calculator_record()
    for snapshot in (
        ScenarioSnapshot(stimulus=None, preconditions=({"operation": "add"},)),
        ScenarioSnapshot(stimulus={"left": float("nan")}, preconditions=({"operation": "add"},)),
        ScenarioSnapshot(stimulus={"left": 2}, preconditions=()),
    ):
        with pytest.raises(CompilationInputError):
            compiler.compile(record.model_copy(update={"scenario_snapshot": snapshot}))
    with pytest.raises(CompilationInputError):
        PytestCompiler(max_source_bytes=262144, max_scenario_bytes=1).compile(record)
    with pytest.raises(GeneratedSafetyError):
        PytestCompiler(max_source_bytes=1, max_scenario_bytes=16000).compile(record)


@pytest.mark.parametrize(
    "extra",
    [
        "import os\n",
        "__import__('os')\n",
        "eval('1')\n",
        "exec('1')\n",
        "import subprocess\n",
        "subprocess.run('x')\n",
        "open('file', 'w')\n",
        "class Injected: pass\n",
        "while True: pass\n",
        "# modified artifact\n",
    ],
)
def test_static_safety_rejects_imports_dynamic_calls_and_changed_structure(compiler, extra):
    artifact = compiler.compile(calculator_record())
    with pytest.raises(GeneratedSafetyError):
        validate_source(
            artifact.source_code + extra,
            expected_source=artifact.source_code,
            test_name=artifact.test_name,
            max_source_bytes=262144,
        )


def test_syntax_and_wrong_function_are_distinct_failures(compiler):
    artifact = compiler.compile(calculator_record())
    with pytest.raises(GeneratedSyntaxError):
        validate_source(
            "def broken(:",
            expected_source=artifact.source_code,
            test_name=artifact.test_name,
            max_source_bytes=262144,
        )
    with pytest.raises(GeneratedSafetyError):
        validate_source(
            artifact.source_code.replace(artifact.test_name, "test_wrong"),
            expected_source=artifact.source_code,
            test_name=artifact.test_name,
            max_source_bytes=262144,
        )


@pytest.mark.parametrize("statement", ["import os", "eval('1')", "observation.__class__"])
def test_static_allowlists_reject_unsafe_nodes_even_with_matching_expected_source(
    compiler, statement
):
    artifact = compiler.compile(calculator_record())
    source = artifact.source_code + f"    {statement}\n"
    with pytest.raises(GeneratedSafetyError):
        validate_source(
            source,
            expected_source=source,
            test_name=artifact.test_name,
            max_source_bytes=262144,
        )


def test_compilation_has_no_harness_provider_or_generated_module_execution(compiler, monkeypatch):
    def prohibited(*args, **kwargs):
        raise AssertionError("Compilation attempted an execution boundary")

    from tests.fixtures.replayable import ControlledCalculatorHarness

    monkeypatch.setattr(ControlledCalculatorHarness, "invoke", prohibited)
    monkeypatch.setattr("app.integrations.llm.openai.OpenAILLMClient.generate", prohibited)
    monkeypatch.setattr("app.integrations.llm.openai.OpenAILLMClient.attribute", prohibited)
    monkeypatch.setattr("builtins.exec", prohibited)
    compiler.compile(calculator_record())
