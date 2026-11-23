"""Trusted PID 1 supervisor. This file runs only inside the ephemeral Linux sandbox."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import BinaryIO


def main() -> None:
    source_limit = int(os.environ["TRACEGUARD_SOURCE_LIMIT"])
    output_limit = int(os.environ["TRACEGUARD_OUTPUT_LIMIT"])
    result_limit = int(os.environ["TRACEGUARD_RESULT_LIMIT"])
    timeout = int(os.environ["TRACEGUARD_TIMEOUT"])
    source = sys.stdin.buffer.read(source_limit + 1)
    if len(source) > source_limit:
        raise ValueError("Source exceeds runner bound")
    bundle = Path("/tmp/bundle")
    bundle.mkdir(mode=0o700)
    test = bundle / "test_generated.py"
    test.write_bytes(source)
    test.chmod(0o400)
    (bundle / "conftest.py").write_bytes(Path("/opt/runner/conftest.py").read_bytes())
    result = Path("/tmp/junit.xml")
    # No spec values contribute arguments or paths. Disable third-party plugin discovery.
    command = [
        sys.executable,
        "-m",
        "pytest",
        "/tmp/bundle/test_generated.py",
        "--junitxml=/tmp/junit.xml",
        "-q",
        "-s",
        "--tb=no",
        "-p",
        "no:cacheprovider",
        "-o",
        "junit_family=xunit1",
        "--confcutdir=/tmp/bundle",
    ]
    child = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH": "/opt/runtime",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONHASHSEED": "0",
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "TRACEGUARD_VARIANT": os.environ["TRACEGUARD_VARIANT"],
            "TRACEGUARD_REPLAY_FIXTURES": os.environ.get("TRACEGUARD_REPLAY_FIXTURES", "{}"),
        },
    )
    exceeded = threading.Event()

    def drain(stream: BinaryIO) -> None:
        total = 0
        while chunk := stream.read(4096):
            total += len(chunk)
            if total > output_limit:
                exceeded.set()
                child.kill()
        stream.close()

    assert child.stdout is not None and child.stderr is not None
    readers = [
        threading.Thread(target=drain, args=(stream,), daemon=True)
        for stream in (child.stdout, child.stderr)
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        child.kill()
        child.wait(timeout=2)
    for reader in readers:
        reader.join(timeout=2)
    xml = None
    if result.is_file():
        with result.open("rb") as file:
            data = file.read(result_limit + 1)
        if len(data) <= result_limit:
            xml = data.decode("utf-8", errors="strict")
    print(
        json.dumps(
            {
                "exit_code": child.returncode,
                "timed_out": timed_out,
                "output_truncated": exceeded.is_set(),
                "junit": xml,
            }
        )
    )


if __name__ == "__main__":
    main()
