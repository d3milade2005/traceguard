import hashlib
import json
from typing import get_args

from pydantic import BaseModel, ValidationError
from pydantic_core.core_schema import ErrorType

from app.schemas.llm_diagnostics import OutputDiagnostics, OutputValidationIssue


def _field_names(value: object) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for child in value.values():
            names.update(_field_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(_field_names(child))
    return names


def output_diagnostics(
    contract: type[BaseModel],
    prompt_version: str,
    text: str,
    error: ValidationError | None,
) -> OutputDiagnostics:
    """Only schema-owned path names and built-in error codes may leave this boundary."""
    schema = contract.model_json_schema()
    names = _field_names(schema)
    errors = (
        error.errors(include_url=False, include_context=False, include_input=False) if error else []
    )
    categories = set(get_args(ErrorType))
    issues = []
    for item in errors[:20]:
        path: list[str | int] = []
        for part in item["loc"][:16]:
            if type(part) is int:
                path.append(part if 0 <= part <= 10000 else "<index>")
            else:
                path.append(part if part in names else "<field>")
        issues.append(
            OutputValidationIssue(
                category=item["type"] if item["type"] in categories else "custom_validation",
                path=tuple(path),
            )
        )
    return OutputDiagnostics(
        schema_name=contract.__name__,  # type: ignore[arg-type]  # Only the two provider contracts.
        schema_sha256=hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
        prompt_version=prompt_version,
        response_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        category="schema_validation" if error else "missing_output",
        error_count=len(errors),
        issues=tuple(issues),
        omitted_issue_count=max(0, len(errors) - 20),
    )
