"""Record offline verification commands and their actual outputs; never enables paid tests."""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from evaluation.artifacts import timestamp, write_once
from evaluation.config import ROOT


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Verification output already exists; use a new versioned directory")
    args.output.mkdir(parents=True)
    env = os.environ.copy()
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONUNBUFFERED="1", DEBUG="false")
    for name in list(env):
        if name.startswith("RUN_LIVE_") or name.startswith("RUN_") and "SANDBOX" in name:
            env.pop(name)
    # Offline tests install their own fakes. No real provider credential is inherited.
    env.pop("OPENAI_API_KEY", None)
    python = sys.executable
    uv = shutil.which("uv") or str(ROOT / ".tools/bin/uv.exe")
    commands = [
        ("lock", [uv, "lock", "--check", "--offline", "--cache-dir", ".local/uv-cache"]),
        (
            "dependencies",
            [uv, "pip", "check", "--python", python, "--cache-dir", ".local/uv-cache"],
        ),
        ("lint", [python, "-m", "ruff", "check", "."]),
        ("format", [python, "-m", "ruff", "format", "--check", "."]),
        ("types", [python, "-m", "mypy", "app"]),
        (
            "offline-tests",
            [
                python,
                "-m",
                "pytest",
                "-o",
                "faulthandler_timeout=60",
                "-m",
                "not live_llm and not sandbox",
                "-q",
                "-p",
                "no:cacheprovider",
                f"--junitxml={args.output / 'offline-tests.xml'}",
            ],
        ),
        (
            "context-audit",
            [
                python,
                "-m",
                "scripts.evaluate_attribution",
                "--context-only",
                "--output",
                str(args.output / "context-audit"),
            ],
        ),
    ]
    results = []
    for name, command in commands:
        started_at = timestamp()
        started = time.monotonic()
        print(f"Starting {name}", flush=True)
        with (
            (args.output / f"{name}.log").open("xb") as log,
            subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
                if os.name == "nt"
                else 0,
            ) as process,
        ):
            try:
                exit_code = process.wait(timeout=900 if name == "offline-tests" else 180)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    # The venv launcher owns a Python child on Windows. Reap the
                    # entire process tree created by this command, not only its stub.
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
                exit_code = 124
        record = {
            "name": name,
            "command": command,
            "cwd": str(ROOT),
            "started_at": started_at,
            "finished_at": timestamp(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "exit_code": exit_code,
            "test_database_configured": bool(env.get("TEST_DATABASE_URL")),
            "test_broker_configured": bool(env.get("TEST_RABBITMQ_URL")),
            "paid_tests_enabled": False,
        }
        results.append(record)
        write_once(args.output / f"{name}.json", record)
        print(f"Finished {name}: exit {exit_code}", flush=True)
    write_once(args.output / "summary.json", {"commands": results})
    if any(r["exit_code"] for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
