"""Build the research-only harness overlay over a verified local production sandbox."""

import argparse
import shutil
import tempfile
from pathlib import Path

from app.integrations.execution.docker_runner import DockerValidationRunner, docker_command
from evaluation.config import ROOT
from evaluation.sandbox import SuiteDockerRunner, suite_bundle_digest
from scripts.evaluate_regressions import sandbox_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", default="traceguard-validation:1")
    parser.add_argument("--suite-image", default="traceguard-evaluation:2")
    args = parser.parse_args()
    base = DockerValidationRunner(sandbox_settings()).prepare(args.base_image)
    command = [
        "build",
        "--pull=false",
        "-f",
        "evaluation/sandbox/Dockerfile",
        "--build-arg",
        f"BASE_IMAGE={args.base_image}",
        "--build-arg",
        f"BASE_IMAGE_ID={base.image_id}",
        "--build-arg",
        f"SUITE_DIGEST={suite_bundle_digest()}",
        "-t",
        args.suite_image,
    ]
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    # A minimal temporary context also works with the host's legacy Docker builder.
    with tempfile.TemporaryDirectory(prefix="evaluation-build-", dir=local) as directory:
        context = Path(directory).resolve()
        if not context.is_relative_to(local.resolve()):
            raise ValueError("Temporary build context escaped the workspace")
        for path in (
            "evaluation/__init__.py",
            "evaluation/harness.py",
            "evaluation/sandbox/conftest.py",
            "evaluation/sandbox/Dockerfile",
            "app/integrations/execution/sandbox/conftest.py",
        ):
            target = context / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, target)
        result = docker_command([*command, str(context)], timeout=300, limit=65536)
    if result.code or result.timed_out or result.exceeded:
        print(result.stderr.decode("utf-8", errors="replace")[-8000:])
        raise RuntimeError("Evaluation image build failed")
    runner = SuiteDockerRunner(
        sandbox_settings(), base_image=args.base_image, suite_image=args.suite_image
    )
    if runner.base_runtime.image_id != base.image_id:
        raise RuntimeError("Base image changed during build")
    print(runner.identity())


if __name__ == "__main__":
    main()
