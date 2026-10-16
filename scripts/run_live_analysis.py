"""Opt-in, one controlled execution through the running product API.

Run with `python -m scripts.run_live_analysis --live`. Resuming --run-id never
creates another run. Generated Python is retrieved only, never executed here.
"""

import argparse
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

from app.integrations.execution.controlled_system import execute_calculator_system


def request(base: str, path: str, *, body: bytes | None = None) -> dict:
    req = Request(base + path, data=body, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def wait_for(base: str, path: str, active: set[str]) -> dict:
    deadline = time.monotonic() + 600
    previous = None
    while time.monotonic() < deadline:
        record = request(base, path)
        progress = record["status"], record.get("stage"), record.get("stop_reason")
        if progress != previous:
            print(progress, flush=True)
            previous = progress
        if record["status"] not in active:
            return record
        time.sleep(2)
    raise TimeoutError("Polling expired; resume using the printed run ID")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--run-id")
    parser.add_argument("--report", type=Path, default=Path(".local/final-e2e.json"))
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    if args.run_id:
        run_id = args.run_id
    else:
        trace = execute_calculator_system("faulty")
        if trace.steps[-1].output == {"answer": 4}:
            raise RuntimeError("Controlled faulty execution did not fail its external oracle")
        raw = trace.model_dump_json(indent=2).encode()
        submitted = request(base, "/api/v1/runs?source=controlled.calculator", body=raw)
        run_id = submitted["id"]
    print("Run ID:", run_id, flush=True)
    run = wait_for(base, f"/api/v1/runs/{run_id}", {"pending", "queued", "processing"})
    if run["status"] != "normalized":
        raise RuntimeError("Normalization failed; no analysis requested")
    path = f"/api/v1/runs/{run_id}/analysis"
    analysis = request(base, path, body=b"")
    if analysis["model"] != "gpt-5.6-luna" or analysis["reasoning_effort"] != "medium":
        raise RuntimeError("Server model configuration differs from the requested live test")
    analysis = wait_for(base, path, {"active"})
    report = {"run": run, "analysis": analysis}
    for key, route in (
        ("attribution_id", "attributions"),
        ("regression_spec_id", "regression-specs"),
        ("generated_test_id", "generated-tests"),
        ("validation_id", "validations"),
    ):
        if analysis[key]:
            report[route] = request(base, f"/api/v1/{route}/{analysis[key]}")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Report:", args.report, flush=True)
    if analysis["status"] != "completed" or analysis["stop_reason"] != "valid":
        raise RuntimeError("Analysis stopped without a valid regression; inspect the saved report")
    for stage in ("attributions", "regression-specs"):
        if not report[stage]["response_id"]:
            raise RuntimeError("Missing live provider response ID")
    print("Persisted verdict=valid; two provider responses recorded.", flush=True)


if __name__ == "__main__":
    main()
