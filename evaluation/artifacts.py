"""Immutable experiment files and a fail-closed, incremental paid-call journal."""

import hashlib
import json
import logging
import os
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def serialized(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2) + "\n"


def digest(value: object) -> str:
    return hashlib.sha256(serialized(value).encode()).hexdigest()


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path.name}")
    return value


def write_once(path: Path, value: object) -> None:
    """Exclusive creation prevents accidental replacement, including across resume."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(serialized(value))
        stream.flush()
        os.fsync(stream.fileno())


def ensure_snapshot(path: Path, value: object) -> None:
    if path.exists():
        if read_json(path) != value:
            raise ValueError(f"Frozen artifact differs: {path.name}")
    else:
        write_once(path, value)


class Experiment:
    def __init__(self, root: Path, config: dict, *, resume: bool) -> None:
        self.root = root
        self.config = config
        self.id = digest(config)
        if root.exists() and not resume:
            raise ValueError("Output exists; use --resume or a new output directory")
        if resume and not (root / "config.json").is_file():
            raise ValueError("Resume requires an existing experiment configuration")
        root.mkdir(parents=True, exist_ok=True)
        ensure_snapshot(root / "config.json", config)

    @contextmanager
    def locked(self) -> Iterator[None]:
        path = self.root / ".lock"
        # A crashed lock is intentionally not auto-stolen. Inspect before removing it.
        with path.open("x", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        try:
            yield
        finally:
            path.unlink()

    def result_path(self, sample_id: str) -> Path:
        return self.root / "results" / f"{digest(sample_id)}.json"

    def completed(self, sample_id: str) -> dict | None:
        path = self.result_path(sample_id)
        if not path.exists():
            return None
        result = read_json(path)
        if result.get("sample_id") != sample_id or result.get("experiment_id") != self.id:
            raise ValueError("Sample result belongs to a different sample or experiment")
        return result

    def save(self, sample_id: str, value: dict) -> None:
        write_once(
            self.result_path(sample_id),
            value | {"experiment_id": self.id, "sample_id": sample_id, "timestamp": timestamp()},
        )

    def results(self) -> list[dict]:
        return [read_json(path) for path in sorted((self.root / "results").glob("*.json"))]

    def usages(self) -> list[dict]:
        """Count paid responses of partial samples independently of sample completion."""
        result = []
        for suffix in ("response", "error"):
            for path in sorted((self.root / "calls").glob(f"*.{suffix}.json")):
                usage = read_json(path).get("usage")
                if usage is not None:
                    result.append(usage)
        return result

    @property
    def calls_started(self) -> int:
        return len(list((self.root / "calls").glob("*.intent.json")))

    def call(
        self,
        sample_id: str,
        stage: str,
        request: BaseModel,
        response_type: type[T],
        invoke: Callable[[], T],
        *,
        max_calls: int,
    ) -> T:
        key = digest([sample_id, stage])
        folder = self.root / "calls"
        intent = folder / f"{key}.intent.json"
        response = folder / f"{key}.response.json"
        error = folder / f"{key}.error.json"
        frozen = {
            "sample_id": sample_id,
            "stage": stage,
            "input_digest": digest(request.model_dump(mode="json")),
        }
        if intent.exists():
            if read_json(intent) != frozen:
                raise ValueError("Recorded call input differs; use a new experiment")
            if response.exists():
                return response_type.model_validate_json(response.read_text(encoding="utf-8"))
            if error.exists():
                raise RecordedCallFailure(read_json(error))
            raise UncertainCall("Call started without a saved response; no automatic paid retry")
        if self.calls_started >= max_calls:
            raise CallLimit("Experiment call limit reached")
        write_once(intent, frozen)
        try:
            result = invoke()
        except Exception as exc:
            # Architectural boundary: preserve a terminal, safe error without exception text.
            logging.getLogger(__name__).error(
                "Experiment call failed: sample=%s stage=%s type=%s stack=%s",
                sample_id,
                stage,
                type(exc).__name__,
                "".join(traceback.format_tb(exc.__traceback__)),
            )
            failure = safe_failure(exc)
            write_once(error, failure)
            raise RecordedCallFailure(failure) from exc
        write_once(response, result.model_dump(mode="json"))
        return result


class CallLimit(RuntimeError):
    pass


class UncertainCall(RuntimeError):
    pass


class RecordedCallFailure(RuntimeError):
    def __init__(self, failure: dict) -> None:
        self.failure = failure
        super().__init__(failure["code"])


def safe_failure(exc: Exception) -> dict:
    from app.core.exceptions import ApplicationError, LLMOutputError

    failure = {
        "code": exc.code if isinstance(exc, ApplicationError) else "EXPERIMENT_ERROR",
        "type": type(exc).__name__,
    }
    if isinstance(exc, LLMOutputError) and exc.usage is not None:
        failure["usage"] = exc.usage.model_dump(mode="json")
    if isinstance(exc, LLMOutputError) and exc.diagnostics is not None:
        failure["diagnostics"] = exc.diagnostics.model_dump(mode="json")
    return failure


def write_report(root: Path, report: dict) -> None:
    """Each distinct report is immutable; identical resumes reproduce the same bytes."""
    name = f"report-{digest(report)[:16]}"
    ensure_snapshot(root / f"{name}.json", report)
    path = root / f"{name}.md"
    if not path.exists():
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(human_report(report))


def human_report(report: dict) -> str:
    lines = ["# Experiment report", "", f"Details: report-{digest(report)[:16]}.json", ""]
    if "metrics" not in report:
        lines.extend(f"- {key}: {value}" for key, value in report.items())
        return "\n".join(lines) + "\n"
    lines.extend([f"Track: {report['track']}", "", "| Metric | Result |", "| --- | --- |"])
    if "planned_samples" in report:
        lines[4:4] = [
            f"Completed: {report['completed_samples']}/{report['planned_samples']}; "
            f"remaining: {report['remaining_samples']}.",
            "",
        ]
    for key, value in report["metrics"].items():
        if isinstance(value, dict) and "accuracy" in value:
            shown = f"{value['correct']}/{value['eligible']} ({value['accuracy']})"
        elif isinstance(value, dict) and "rate" in value:
            shown = f"{value['count']} ({value['rate']})"
        elif isinstance(value, (dict, list)):
            continue
        else:
            shown = str(value)
        lines.append(f"| {key} | {shown} |")
    for key in ("anomalies", "partition_counts", "usage"):
        if key in report:
            lines.extend(["", f"{key}: {json.dumps(report[key], sort_keys=True)}"])
    lines.extend(
        ["", "Rates use the explicit denominators in JSON; null means no eligible observations."]
    )
    return "\n".join(lines) + "\n"
