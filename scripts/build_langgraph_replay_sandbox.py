"""Build the fixed research-only LangGraph overlay with complete build evidence."""

import argparse
import hashlib
import shutil
from pathlib import Path

from app.integrations.execution.docker_runner import DockerValidationRunner, docker_command
from evaluation.artifacts import timestamp, write_once
from evaluation.config import ROOT
from evaluation.langgraph_replay import (
    OVERLAY_FILES,
    LangGraphDockerRunner,
    replay_bundle_digest,
)
from scripts.evaluate_regressions import sandbox_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--replay-image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    context = args.output / "context"
    hashes = {}
    for name in OVERLAY_FILES:
        target = context / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
        hashes[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    frozen_digest = replay_bundle_digest()
    base = DockerValidationRunner(sandbox_settings()).prepare(args.base_image)
    command = [
        "build",
        "--pull=false",
        "-f",
        str(context / "evaluation/langgraph_sandbox/Dockerfile"),
        "--build-arg",
        "BASE_IMAGE=" + base.image_id,
        "--build-arg",
        "BASE_IMAGE_ID=" + base.image_id,
        "--build-arg",
        "REPLAY_DIGEST=" + frozen_digest,
        "-t",
        args.replay_image,
        str(context),
    ]
    started = timestamp()
    result = docker_command(command, timeout=300, limit=1000000)
    write_once(
        args.output / "build.json",
        {
            "command": ["docker", *command],
            "source_hashes": hashes,
            "started_at": started,
            "finished_at": timestamp(),
            "code": result.code,
            "timed_out": result.timed_out,
            "exceeded": result.exceeded,
            "stdout": result.stdout.decode(errors="replace"),
            "stderr": result.stderr.decode(errors="replace"),
        },
    )
    if result.code or result.timed_out or result.exceeded:
        raise RuntimeError("Research overlay build failed; inspect saved build evidence")
    if replay_bundle_digest() != frozen_digest:
        raise ValueError("Replay source changed during build; do not use this image")
    runner = LangGraphDockerRunner(
        sandbox_settings(), base_image=args.base_image, replay_image=args.replay_image
    )
    write_once(args.output / "identity.json", runner.identity())
    print(runner.identity())


if __name__ == "__main__":
    main()
