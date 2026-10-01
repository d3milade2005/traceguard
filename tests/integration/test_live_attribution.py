"""One paid request, only with explicit opt-in and a dedicated PostgreSQL test DB."""

import json
from pathlib import Path

import pytest
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import get_settings
from app.integrations.llm.openai import OpenAILLMClient
from app.integrations.traces.sources import trace_adapters
from app.schemas.runs import RunCreate
from app.services.attribution import AttributionService
from app.services.attributions import AttributionStore
from app.services.context import ContextManager
from app.services.normalization import NormalizationService
from app.services.runs import RunService

ROOT = Path(__file__).resolve().parents[2]


class LiveSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    run_live_llm_tests: bool = False
    openai_api_key: SecretStr | None = None


# Capture before the suite's isolation fixture clears application configuration.
LIVE = LiveSettings()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.live_llm,
    pytest.mark.skipif(
        not LIVE.run_live_llm_tests or not LIVE.openai_api_key,
        reason="Set RUN_LIVE_LLM_TESTS=true and OPENAI_API_KEY to opt into one paid request",
    ),
]


def test_one_small_who_when_attribution_persists(postgres):
    _, sessions, _ = postgres
    settings = get_settings()
    rows = json.loads(
        (ROOT / "tests/fixtures/who_when/algorithm_generated_sample.json").read_text(
            encoding="utf-8"
        )
    )
    # Select one committed record by execution size only, never by its benchmark labels.
    row = min(rows, key=lambda item: len(json.dumps(item["history"])))
    raw = json.dumps({"question": row["question"], "history": row["history"]}).encode()
    runs = RunService(sessions, settings.max_request_body_bytes)
    run = runs.create(
        RunCreate(
            source="who_when.algorithm_generated",
            original_trace=raw,
            failure_signal="developer_report",
        )
    )
    NormalizationService(
        sessions,
        trace_adapters(),
        lease_seconds=90,
        max_trace_bytes=settings.max_request_body_bytes,
    ).process(run.id)
    store = AttributionStore(sessions)

    class ExplicitPublisher:
        def publish(self, attribution_id, request_id):
            # This opt-in test processes synchronously, without any automatic retry.
            assert store.get(attribution_id).status == "pending"

    accepted, _ = store.submit(
        run.id,
        "live-attribution",
        ExplicitPublisher(),
        model="gpt-5.6-luna",
        reasoning_effort="medium",
    )
    service = AttributionService(
        sessions,
        runs,
        ContextManager(settings.analysis_context),
        OpenAILLMClient(
            LIVE.openai_api_key,
            timeout_seconds=settings.openai_timeout_seconds,
            max_output_tokens=settings.openai_max_output_tokens,
        ),
        lease_seconds=settings.openai_timeout_seconds + 60,
    )
    service.process(accepted.id, final_attempt=True)
    result = store.get(accepted.id)
    assert result.status == "completed", result.failure_code
    assert result.decision in {"attributed", "abstain"}
    assert result.response_id and result.input_tokens is not None
    assert result.output_tokens is not None
    visible = set(result.context_manifest["visible_sequences"])
    assert result.decisive_sequence is None or result.decisive_sequence in visible
    for reference in result.evidence:
        assert set(range(reference.start_sequence, reference.end_sequence + 1)) <= visible
