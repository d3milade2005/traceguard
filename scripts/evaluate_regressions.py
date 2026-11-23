"""Offline-by-default controlled regression synthesis experiments (separate from Who&When)."""

import argparse
import json
from pathlib import Path

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from app.core.config import Settings
from app.core.context import ContextPolicy
from app.integrations.llm.openai import OpenAILLMClient
from app.services.context import ContextManager
from evaluation.artifacts import (
    CallLimit,
    Experiment,
    UncertainCall,
    digest,
    ensure_snapshot,
    read_json,
    write_report,
)
from evaluation.config import ProviderSettings, inference_config
from evaluation.metrics import build_report
from evaluation.regressions import evaluate_scenario
from evaluation.sandbox import SuiteDockerRunner
from evaluation.scenarios import controlled_suite, suite_manifest


class EvaluationSandboxSettings(Settings):
    """Keep the sandbox policy reproducible and independent of host application settings."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def sandbox_settings() -> Settings:
    # No database or broker is contacted. These explicit non-routable values satisfy
    # the existing sandbox settings contract without reading application secrets.
    return EvaluationSandboxSettings(
        _env_file=None,
        database_url="postgresql+psycopg://unused:unused@127.0.0.1:1/unused",
        rabbitmq_url="amqp://unused:unused@127.0.0.1:1/unused",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-samples", type=int, default=6)
    parser.add_argument("--max-calls", type=int, default=0)
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument(
        "--reasoning-effort", choices=("low", "medium", "high", "xhigh"), default="medium"
    )
    parser.add_argument("--base-image", default="traceguard-validation:1")
    parser.add_argument("--suite-image", default="traceguard-evaluation:2")
    parser.add_argument("--context-policy", type=Path)
    args = parser.parse_args()
    if args.max_samples < 1 or args.max_calls < 0 or (args.live and args.dry_run):
        parser.error("Invalid mode or limits")
    suite = controlled_suite()
    expected = suite_manifest()
    if args.live and args.manifest is None:
        parser.error("Live runs require the explicit suite manifest produced by a dry run")
    manifest = read_json(args.manifest) if args.manifest else expected
    if manifest != expected:
        parser.error("Suite manifest differs from the checked-in implementations/oracles")
    scenarios = suite[: args.max_samples]
    for scenario in scenarios:
        scenario.check_actual_failure()
    provider = ProviderSettings()
    policy = (
        ContextPolicy.model_validate(read_json(args.context_policy))
        if args.context_policy
        else ContextPolicy()
    )
    config = {
        "track": "regressions",
        "inference": inference_config(
            args.model, args.reasoning_effort, policy, provider, track="regressions"
        ),
        "manifest_digest": digest(manifest),
        "samples": [s.sample_id for s in scenarios],
        "base_image": args.base_image,
        "suite_image": args.suite_image,
    }
    experiment = Experiment(args.output, config, resume=args.resume)
    with experiment.locked():
        ensure_snapshot(args.output / "suite-manifest.json", manifest)
        if not args.live:
            plan = {
                "mode": "dry-run",
                "scenarios": len(scenarios),
                "planned_calls_upper_bound": len(scenarios) * 2,
                "model": args.model,
                "reasoning_effort": args.reasoning_effort,
                "actual_faulty_and_corrected_implementations_checked": True,
            }
            write_report(args.output, plan)
            print(json.dumps(plan, indent=2))
            return
        if args.max_calls < 1 or provider.openai_api_key is None:
            parser.error("Live inference requires a positive --max-calls and OPENAI_API_KEY")
        complete = sum(experiment.completed(s.sample_id) is not None for s in scenarios)
        print(
            {
                "planned_calls_upper_bound": len(scenarios) * 2,
                "already_completed_samples": complete,
                "remaining_samples": len(scenarios) - complete,
                "calls_already_started": experiment.calls_started,
                "max_calls_total": args.max_calls,
                "model": args.model,
                "reasoning_effort": args.reasoning_effort,
            },
            flush=True,
        )
        validator = SuiteDockerRunner(
            sandbox_settings(), base_image=args.base_image, suite_image=args.suite_image
        )
        # Bind exact image IDs before the first paid call; mutable tags cannot drift on resume.
        ensure_snapshot(args.output / "runtime.json", validator.identity())
        client = OpenAILLMClient(
            provider.openai_api_key,
            timeout_seconds=provider.openai_timeout_seconds,
            max_output_tokens=provider.openai_max_output_tokens,
        )
        for scenario in scenarios:
            if experiment.completed(scenario.sample_id) is not None:
                continue
            try:
                row = evaluate_scenario(
                    scenario,
                    ContextManager(policy),
                    client,
                    client,
                    validator,
                    experiment,
                    model=args.model,
                    effort=args.reasoning_effort,
                    max_calls=args.max_calls,
                )
            except (CallLimit, UncertainCall) as exc:
                print(str(exc), flush=True)
                break
            experiment.save(scenario.sample_id, row)
        rows = experiment.results()
        usages = experiment.usages()
        report = build_report(rows, "regressions", planned=len(scenarios), usages=usages)
        report["calls_started"] = experiment.calls_started
        report["experiment_id"] = experiment.id
        report["configuration"] = experiment.config
        write_report(args.output, report)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
