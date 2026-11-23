"""Approved prospective evaluation. Live generations require --live and a sealed freeze.

Never executes generated code on the host. The durable service coordinator is driven
synchronously; transport delivery is recorded, not claimed as a new HTTP/Celery test.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema

from app.core.config import Settings
from app.db import models
from app.db.session import create_session_factory
from app.integrations.execution.contracts import ExecutionBundle
from app.integrations.execution.docker_runner import docker_command
from app.integrations.execution.registry import bundle_digest
from app.integrations.traces.sources import trace_adapters
from app.schemas.runs import RunCreate
from app.services.analyses import AnalysisService
from app.services.attribution import AttributionService
from app.services.attributions import AttributionStore
from app.services.context import ContextManager
from app.services.generated_tests import GeneratedTestService
from app.services.normalization import NormalizationService
from app.services.pytest_compiler import PytestCompiler
from app.services.regression import RegressionService
from app.services.regression_specs import RegressionStore
from app.services.runs import RunService
from app.services.validations import ValidationService
from evaluation.fresh_provider import Journal, StudyProvider, open_client, write
from evaluation.fresh_sandbox import FreshSandbox
from examples.langgraph_refund.fresh_application import SCENARIOS, execute

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / ".local/langgraph-fresh-e2e-2026-11-23"
IMAGE = "traceguard-fresh-nodes:20261123"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files():
    paths = []
    for name in ("app", "examples", "evaluation", "scripts", "tests", "alembic"):
        paths += [
            p for p in (ROOT / name).rglob("*") if p.is_file() and "__pycache__" not in p.parts
        ]
    paths += [
        ROOT / n
        for n in (
            "pyproject.toml",
            "uv.lock",
            "validation.Dockerfile",
            "alembic.ini",
            "AGENTS.md",
            "README.md",
        )
    ]
    return sorted(paths)


def verify_freeze(root):
    frozen = json.loads((root / "freeze.json").read_text())
    for name, digest in frozen["source"].items():
        if sha(ROOT / name) != digest:
            raise RuntimeError("Frozen source changed: " + name)
    return frozen


def database(root):
    # Use an already dedicated test database, but a NEW retained schema. No old data touched.
    config = dotenv_values(ROOT / ".env")
    old = json.loads(
        (
            ROOT / ".local/experiments/who-when-terra-high-2026-11-23/verification-context.json"
        ).read_text()
    )
    url = make_url(config["DATABASE_URL"]).set(
        host="127.0.0.1", port=5433, database=old["database"]
    )
    if not url.database.endswith("_test"):
        raise RuntimeError("Dedicated test database required")
    context = root / "database-context.json"
    if context.exists():
        schema = json.loads(context.read_text())["schema"]
    else:
        schema = "traceguard_fresh_" + uuid4().hex
        engine = create_engine(url, hide_parameters=True)
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        engine.dispose()
        write(
            context,
            {
                "schema": schema,
                "database": url.database,
                "host": "127.0.0.1",
                "port": 5433,
                "retained": True,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
    scoped = url.update_query_dict({"options": "-csearch_path=" + schema})
    os.environ["DATABASE_URL"] = scoped.render_as_string(hide_password=False)
    engine = create_engine(scoped, hide_parameters=True)
    migration = Config(str(ROOT / "alembic.ini"))
    migration.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(migration, "head")
    return engine, create_session_factory(engine)


class Publisher:
    def __init__(self):
        self.calls = []

    def publish(self, identifier, request_id):
        self.calls.append((identifier, request_id))


def export_database(sessions, directory):
    export = {}
    # Synthetic experiment-only schema; no credentials or unrelated application records.
    with sessions() as session:
        for table in models.Run.metadata.sorted_tables:
            records = []
            for row in session.execute(table.select()).mappings():
                converted = {}
                for key, value in row.items():
                    if isinstance(value, bytes):
                        converted[key] = {
                            "utf8": value.decode(),
                            "sha256": hashlib.sha256(value).hexdigest(),
                        }
                    elif isinstance(value, (datetime, UUID)):
                        converted[key] = str(value)
                    else:
                        converted[key] = value
                records.append(converted)
            export[table.name] = records
    write(directory / "database-export.json", export)


def pipeline(root, sessions, settings, case, target, provider, envelope):
    path = root / "cases" / case
    runs = RunService(sessions, settings.max_request_body_bytes)
    raw = json.dumps(envelope, ensure_ascii=False, indent=2).encode()
    run = runs.create_idempotent(
        RunCreate(
            source="langgraph",
            original_trace=raw,
            failure_signal=envelope["failure"]["message"],
            request_id=case,
        ),
        uuid5(NAMESPACE_URL, str(root) + "/" + case),
    )
    NormalizationService(
        sessions,
        trace_adapters(),
        lease_seconds=120,
        max_trace_bytes=settings.max_request_body_bytes,
    ).process(run.id, final_attempt=True)
    attributions, specs = AttributionStore(sessions), RegressionStore(sessions)
    compiler = GeneratedTestService(
        sessions,
        PytestCompiler(
            max_source_bytes=settings.generated_test_max_source_bytes,
            max_scenario_bytes=settings.regression_max_scenario_bytes,
        ),
        runs,
        attributions,
    )
    fixtures = {"calls": provider.fixtures, "injected_agent": target}
    write(path / "provider-fixtures.json", fixtures)
    runner = FreshSandbox(settings, fixtures, path / "sandbox-primary")
    validations = ValidationService(sessions, settings, runner)
    pubs = [Publisher() for _ in range(4)]
    coordinator = AnalysisService(sessions, attributions, specs, compiler, validations, *pubs[:3])
    workers = [
        AttributionService(
            sessions, runs, ContextManager(settings.analysis_context), provider, lease_seconds=150
        ),
        RegressionService(
            sessions,
            runs,
            attributions,
            provider,
            lease_seconds=150,
            max_input_bytes=settings.regression_max_input_bytes,
            max_scenario_bytes=settings.regression_max_scenario_bytes,
        ),
        validations,
    ]
    coordinator.submit(run.id, case, pubs[3], model="gpt-5.6-terra", reasoning_effort="high")
    processed = set()
    for _tick in range(20):
        if not coordinator.advance(run.id):
            break
        for publisher, worker in zip(pubs[:3], workers, strict=True):
            while publisher.calls:
                identifier, _ = publisher.calls.pop(0)
                if identifier not in processed:
                    processed.add(identifier)
                    worker.process(identifier, final_attempt=True)
    record = coordinator.get(run.id)
    outcome = {"analysis": record.model_dump(mode="json"), "run_id": str(run.id)}
    for key, service in (
        ("attribution", attributions),
        ("regression_spec", specs),
        ("generated_test", compiler),
        ("validation", validations),
    ):
        identifier = getattr(record, key + "_id")
        if identifier:
            value = service.get(identifier)
            outcome[key] = value.model_dump(mode="json")
    if record.generated_test_id:
        artifact = compiler.get(record.generated_test_id)
        if artifact.status == "completed":
            # Source field is retrieved from the persisted artifact, never hand-edited.
            bundle = ExecutionBundle(source=artifact.source_code, test_name=artifact.test_name)
            (path / "test_generated.py").write_bytes(artifact.source_code.encode("utf-8"))
            runtime = runner.prepare(settings.validation_image)
            repeats = []
            for repeat in (2, 3):
                repeat_runner = FreshSandbox(settings, fixtures, path / f"sandbox-repeat-{repeat}")
                repeats.append(
                    {
                        variant: repeat_runner.execute(
                            bundle, runtime, variant, uuid4(), uuid4()
                        ).model_dump(mode="json")
                        for variant in ("faulty", "corrected")
                    }
                )
            outcome["additional_pairs"] = repeats
    write(path / "pipeline.json", outcome)
    write(path / "canonical.json", runs.canonical_trace(run.id).model_dump(mode="json"))
    return outcome


def main():
    # The host exports DEBUG=release for another tool; this study explicitly disables debug.
    os.environ["DEBUG"] = "false"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("build", "verify-image", "freeze", "live", "offline-sandbox", "offline-services"),
    )
    parser.add_argument("--output", type=Path, default=DEFAULT)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    settings = Settings(validation_image=IMAGE)
    if args.mode == "verify-image":
        logs = root / f"image-verification-{len(list(root.glob('image-verification*'))) + 1}"
        runtime = FreshSandbox(settings, {}, logs).prepare(IMAGE)
        write(root / "runtime.json", runtime.model_dump(mode="json"))
        print("Current image verified", runtime.image_id, flush=True)
        return
    if args.mode == "build":
        context = root / "docker-build-context"
        files = [
            ROOT / "validation.Dockerfile",
            *[
                p
                for p in source_files()
                if p.as_posix().endswith(
                    (
                        "regression_runtime.py",
                        "execution/calculator.py",
                        "execution/refund.py",
                        "execution/langgraph_refund.py",
                        "sandbox/conftest.py",
                        "sandbox/entrypoint.py",
                        "execution/sandbox/requirements.lock",
                    )
                )
            ],
        ]
        for file in files:
            dest = context / file.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, dest)
        result = docker_command(
            [
                "build",
                "--pull=false",
                "-f",
                "validation.Dockerfile",
                "--build-arg",
                "BUNDLE_DIGEST=" + bundle_digest(),
                "-t",
                IMAGE,
                str(context),
            ],
            timeout=300,
            limit=100000,
        )
        write(
            root / f"image-build-{len(list(root.glob('image-build*.json'))) + 1}.json",
            {
                "code": result.code,
                "stdout": result.stdout.decode(errors="replace"),
                "stderr": result.stderr.decode(errors="replace"),
            },
        )
        if result.code or result.timed_out:
            raise RuntimeError("Image build failed; see saved log")
        logs = root / f"image-verification-{len(list(root.glob('image-verification*'))) + 1}"
        runtime = FreshSandbox(settings, {}, logs).prepare(IMAGE)
        write(root / "runtime.json", runtime.model_dump(mode="json"))
        print("Sandbox image built and verified", flush=True)
        return
    if args.mode in {"offline-sandbox", "offline-services"}:
        from evaluation.fresh_verification import verify

        verify(root, settings, use_services=args.mode == "offline-services")
        return
    if args.mode == "freeze":
        from app.integrations.execution.langgraph_refund import FIELDS, PROMPTS
        from app.services.attribution_input import INSTRUCTIONS as ATTR
        from app.services.regression_input import INSTRUCTIONS as SPEC

        source = {str(p.relative_to(ROOT)).replace("\\", "/"): sha(p) for p in source_files()}
        for path in source:
            dest = root / "source-frozen" / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / path, dest)
        proposal = ROOT / ".local/langgraph-fresh-e2e-proposal-2026-11-23"
        for name in ("PROPOSED_PROTOCOL.txt", "proposed-config.json", "budget-proposal.json"):
            shutil.copy2(proposal / name, root / ("approved-" + name))
        config = json.loads((proposal / "proposed-config.json").read_text())
        if settings.analysis_context.model_dump() != config["context_policy"]:
            raise RuntimeError("Context configuration differs from the approved protocol")
        config.update(status="FROZEN", paid_execution_authorized=True)
        write(root / "frozen-config.json", config)
        write(
            root / "prompts.json",
            {
                "attribution": ATTR,
                "regression": SPEC,
                "application": PROMPTS,
                "application_schemas": FIELDS,
            },
        )
        write(
            root / "freeze.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "git_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], text=True
                ).strip(),
                "source": source,
                "runtime": json.loads((root / "runtime.json").read_text()),
                "scenarios": SCENARIOS,
                "configuration_sha256": sha(root / "frozen-config.json"),
                "prompts_sha256": sha(root / "prompts.json"),
                "command": ".venv/Scripts/python.exe -m scripts.run_fresh_langgraph live --live",
            },
        )
        print("Frozen", sha(root / "freeze.json"), flush=True)
        return
    if not args.live:
        raise RuntimeError("Explicit --live required")
    frozen = verify_freeze(root)
    runtime = FreshSandbox(settings, {}, root / "live-runtime-verification").prepare(IMAGE)
    if runtime.image_id != frozen["runtime"]["image_id"]:
        raise RuntimeError("Runtime changed since freeze")
    # A process lock is never automatically broken. Removing a stale lock requires inspection.
    lock = root / "live.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(descriptor, str(os.getpid()).encode())
    os.close(descriptor)
    engine = None
    try:
        engine, sessions = database(root)
        with open_client(settings.openai_api_key) as client:
            journal = Journal(root / "inference", client)
            for case, target, index in SCENARIOS:
                path = root / "cases" / case
                if (path / "result.json").exists():
                    continue
                if path.exists():
                    raise RuntimeError(
                        "Partial scenario requires offline recovery inspection; no automatic rerun"
                    )
                path.mkdir(parents=True, exist_ok=True)
                outcome = {
                    "id": case,
                    "expected_agent": target,
                    "expected_step": index,
                    "started_at": datetime.now(UTC).isoformat(),
                    "interventions": [],
                }
                print(case, "faulty graph", flush=True)
                try:
                    provider = StudyProvider(journal, case + "-faulty")
                    faulty = execute(target, provider)
                    write(path / "faulty-graph.json", faulty)
                    outcome["faulty_oracle"] = faulty["oracle"]
                    if faulty["envelope"]:
                        outcome["pipeline"] = pipeline(
                            root, sessions, settings, case, target, provider, faulty["envelope"]
                        )
                    else:
                        outcome["pipeline_not_started"] = "No externally observed failed trace"
                    print(case, "corrected fresh graph", flush=True)
                    corrected = execute(
                        target, StudyProvider(journal, case + "-corrected"), corrected=True
                    )
                    write(path / "corrected-graph.json", corrected)
                    outcome["corrected_oracle"] = corrected["oracle"]
                except Exception as exc:
                    # Experimental boundary: retain failed/incomplete scenarios. Never retry.
                    outcome["error"] = {"type": type(exc).__name__}
                    import traceback

                    outcome["error"]["frames"] = [
                        {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                        for frame in traceback.extract_tb(exc.__traceback__)
                    ]
                    print(case, "stopped:", type(exc).__name__, flush=True)
                    if journal.stopped:
                        write(path / "result.json", outcome)
                        break
                outcome["finished_at"] = datetime.now(UTC).isoformat()
                write(path / "result.json", outcome)
        export_database(sessions, root)
    finally:
        if engine:
            engine.dispose()
        lock.unlink()
    print("Execution recorded. No automatic success inference; analyze saved records.", flush=True)


if __name__ == "__main__":
    main()
