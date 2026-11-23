"""Run the external graph, then optionally submit its captured failed execution."""

import argparse
import json
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from examples.langgraph_refund.application import execute_demo
from examples.langgraph_refund.integration import TraceGuardClient


class DemoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.6-luna"
    traceguard_url: str = "http://127.0.0.1:8000"
    traceguard_origin: str = "http://localhost:5173"
    traceguard_session_cookie: SecretStr | None = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument(
        "--auto-analyze",
        action="store_true",
        help="Opt into TraceGuard's paid attribution after normalization",
    )
    parser.add_argument("--live", action="store_true", help="Opt into up to three paid agent calls")
    parser.add_argument("--corrected", action="store_true")
    args = parser.parse_args()
    if args.auto_analyze and not args.submit:
        parser.error("--auto-analyze requires --submit")
    if args.corrected and args.live:
        parser.error("The live experiment has one intentional faulty decision policy")
    settings = DemoSettings()
    if args.submit and not settings.traceguard_session_cookie:
        parser.error("An authenticated TRACEGUARD_SESSION_COOKIE is required")
    if args.output.exists():
        parser.error("Output exists; preserve it and choose a new path")
    agents = None
    if args.live:
        if settings.openai_api_key is None:
            parser.error("OPENAI_API_KEY is required for the explicitly opted-in live mode")
        from examples.langgraph_refund.live_agents import LiveAgents

        agents = LiveAgents(settings.openai_api_key.get_secret_value(), settings.openai_model)
    try:
        recorder, trace, result = execute_demo(corrected=args.corrected, agents=agents)
    finally:
        if agents:
            agents.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Save evidence before any network submission. Retry with the same payload/ID;
    # rerunning the graph creates a different execution and is not an HTTP retry.
    with args.output.open("x", encoding="utf-8") as file:
        file.write(
            json.dumps(
                {
                    "trace": trace,
                    "result": result,
                    "agent_calls": agents.calls if agents else 0,
                    "model_usage": agents.usage if agents else [],
                },
                indent=2,
            )
            + "\n"
        )
    print(
        json.dumps(
            {
                "execution_id": recorder.execution_id,
                "failed": trace is not None,
                "agent_calls": agents.calls if agents else 0,
            }
        )
    )
    if args.submit:
        client = TraceGuardClient(
            settings.traceguard_url,
            settings.traceguard_session_cookie.get_secret_value(),
            origin=settings.traceguard_origin,
        )
        response = client.submit_failed(trace, auto_analyze=args.auto_analyze)
        print(json.dumps(response))


if __name__ == "__main__":
    main()
