"""Opt-in one-request synthesis from an existing real attribution in a test database."""

from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.db.session import create_session_factory
from app.integrations.llm.openai import OpenAILLMClient
from app.services.attributions import AttributionStore
from app.services.regression import RegressionService
from app.services.regression_input import load_evidence, preflight
from app.services.regression_specs import RegressionStore
from app.services.runs import RunService


class LiveSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        extra="ignore",
        hide_input_in_errors=True,
    )
    run_live_regression_tests: bool = False
    live_regression_attribution_id: UUID | None = None
    openai_api_key: SecretStr | None = None
    test_database_url: SecretStr | None = None


LIVE = LiveSettings()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.live_llm,
    pytest.mark.skipif(
        not LIVE.run_live_regression_tests,
        reason="Explicit RUN_LIVE_REGRESSION_TESTS=true required for one paid request",
    ),
]


def test_one_real_completed_attribution_generates_and_persists():
    if (
        not LIVE.openai_api_key
        or not LIVE.test_database_url
        or not LIVE.live_regression_attribution_id
    ):
        pytest.fail(
            "Live smoke requires key, dedicated test database, and completed real attribution ID"
        )
    url = make_url(LIVE.test_database_url.get_secret_value())
    assert url.drivername == "postgresql+psycopg" and (url.database or "").endswith("_test")
    engine = create_engine(url, hide_parameters=True)
    try:
        sessions = create_session_factory(engine)
        attributions = AttributionStore(sessions)
        attribution = attributions.get(LIVE.live_regression_attribution_id)
        assert attribution.status == "completed" and attribution.decision == "attributed"
        assert attribution.response_id and attribution.response_id != "resp_fake"
        store = RegressionStore(sessions)
        settings = get_settings()
        runs = RunService(sessions, settings.max_request_body_bytes)
        evidence = load_evidence(attribution, runs.canonical_trace(attribution.run_id))
        assert preflight(evidence) is None, (
            "Choose an attribution with complete reconstructable input"
        )

        class CountingGenerator:
            calls = 0

            def generate(self, request):
                self.calls += 1
                assert self.calls == 1
                assert request.model == "gpt-5.6-luna" and request.reasoning_effort == "medium"
                return OpenAILLMClient(
                    LIVE.openai_api_key,
                    timeout_seconds=settings.openai_timeout_seconds,
                    max_output_tokens=settings.openai_max_output_tokens,
                ).generate(request)

        generator = CountingGenerator()

        class ExplicitPublisher:
            def publish(self, operation_id, request_id):
                assert store.get(operation_id).status == "pending"

        accepted, created = store.submit(
            attribution.id,
            "live-regression",
            ExplicitPublisher(),
            model="gpt-5.6-luna",
            reasoning_effort="medium",
        )
        # Never accidentally reuse an operation and report it as a fresh live request.
        assert created, "Use an attribution without an existing regression operation"
        service = RegressionService(
            sessions,
            runs,
            attributions,
            generator,
            lease_seconds=settings.openai_timeout_seconds + 60,
            max_input_bytes=settings.regression_max_input_bytes,
            max_scenario_bytes=settings.regression_max_scenario_bytes,
        )
        service.process(accepted.id, final_attempt=True)
        assert generator.calls == 1
        result = store.get(accepted.id)
        assert result.status == "completed", result.failure_code
        assert result.spec and result.disposition in {"synthesizable", "needs_review"}
        assert (
            result.response_id
            and result.input_tokens is not None
            and result.output_tokens is not None
        )
        assert result.reasoning_tokens is not None
        # Strict contract validation rejects source/code fields and executable-looking values.
        from app.schemas.regression_specs import RegressionSpec

        assert RegressionSpec.model_validate_json(result.spec.model_dump_json()) == result.spec
    finally:
        engine.dispose()
