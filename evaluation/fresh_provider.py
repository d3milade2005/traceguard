"""Single-process, fail-closed research inference journal and shared spending guard."""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from openai import APIConnectionError, APIStatusError, OpenAI
from openai.types.responses import Response

from app.integrations.execution.langgraph_refund import request_key
from app.integrations.llm.openai import parse_structured_response
from app.schemas.attributions import AttributionDecision, LLMResult
from app.schemas.regression_specs import RegressionResult, RegressionSpec

LIMITS = {
    "gpt-5.6-luna": (18, 4096, 2048, Decimal("0.25"), Decimal("1.20")),
    "gpt-5.6-terra": (6, 16384, 4096, Decimal("2.50"), Decimal("12")),
}


class GuardStop(RuntimeError):
    pass


def write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.flush()
        import os

        os.fsync(file.fileno())


class Journal:
    def __init__(self, root: Path, client) -> None:
        self.root, self.client = root, client
        root.mkdir(parents=True, exist_ok=True)
        self.stopped = (root / "STOP.json").exists()

    def intents(self):
        return [json.loads(p.read_text()) for p in self.root.glob("*/intent.json")]

    def check(self, key: str, payload: dict, counted: int) -> Decimal:
        if self.stopped or (self.root / "STOP.json").exists():
            raise GuardStop("Journal stopped")
        model = payload["model"]
        maximum, ceiling, output, rate, out_rate = LIMITS[model]
        intents = self.intents()
        if len(intents) >= 24 or sum(i["model"] == model for i in intents) >= maximum:
            raise GuardStop("Request allowance exhausted")
        if (
            type(counted) is not int
            or not 0 <= counted <= ceiling
            or payload["max_output_tokens"] != output
        ):
            raise GuardStop("Token limits exceeded")
        reserve = ((ceiling + 1024) * rate + output * out_rate) / Decimal(1000000)
        if sum(Decimal(i["reservation_usd"]) for i in intents) + reserve > Decimal("0.90"):
            raise GuardStop("Reservation allowance exhausted")
        for path in self.root.glob("*/intent.json"):
            if not (path.parent / "response.json").exists():
                raise GuardStop("Uncertain previous request; no new generation allowed")
        directory = self.root / key
        if directory.exists():
            raise GuardStop("Duplicate dispatch")
        return reserve

    def stop(self, reason: str) -> None:
        self.stopped = True
        if not (self.root / "STOP.json").exists():
            write(self.root / "STOP.json", {"reason": reason, "at": datetime.now(UTC).isoformat()})

    def response(self, key: str, payload: dict) -> tuple[Response, int]:
        directory = self.root / key
        digest = request_key(payload)
        if (directory / "intent.json").exists():
            intent = json.loads((directory / "intent.json").read_text())
            if intent["request_hash"] != digest:
                raise GuardStop("Resume request changed")
            if not (directory / "response.json").exists():
                raise GuardStop("Uncertain dispatch cannot be resent")
            if not (directory / "cost.json").exists():
                raise GuardStop("Saved response requires usage reconciliation before recovery")
            saved = json.loads((directory / "response.json").read_text())
            return Response.model_validate(saved["response"]), saved["latency_ms"]
        # Only the documented count-compatible fields are sent to the count endpoint.
        if len(self.intents()) >= 24:
            raise GuardStop("Request allowance exhausted before token count")
        fields = {
            k: v
            for k, v in payload.items()
            if k
            in {
                "model",
                "instructions",
                "input",
                "text",
                "reasoning",
                "tools",
                "tool_choice",
                "truncation",
            }
        }
        try:
            counted = self.client.responses.input_tokens.count(**fields)
        except (APIConnectionError, APIStatusError):
            self.stop("Token count unavailable")
            raise GuardStop("Token count unavailable") from None
        write(self.root / "counts" / (key + ".json"), counted.model_dump(mode="json"))
        reserve = self.check(key, payload, counted.input_tokens)
        write(directory / "request.json", payload)
        write(
            directory / "intent.json",
            {
                "key": key,
                "request_hash": digest,
                "model": payload["model"],
                "reservation_usd": str(reserve),
                "counted_input_tokens": counted.input_tokens,
                "at": datetime.now(UTC).isoformat(),
            },
        )
        # Recheck the serialized bytes that are actually dispatched.
        if request_key(json.loads((directory / "request.json").read_text())) != digest:
            self.stop("Payload changed after reservation")
            raise GuardStop("Payload mismatch")
        start = time.monotonic()
        try:
            response = self.client.responses.create(**payload)
        except (APIConnectionError, APIStatusError) as exc:
            write(
                directory / "error.json",
                {
                    "type": type(exc).__name__,
                    "status_code": getattr(exc, "status_code", None),
                    "request_id": getattr(exc, "request_id", None),
                    "latency_ms": round((time.monotonic() - start) * 1000),
                },
            )
            self.stop("Uncertain/provider error; no retries")
            raise GuardStop("Provider error; safe details journaled") from None
        latency = round((time.monotonic() - start) * 1000)
        body = response.model_dump(mode="json")
        body["output"] = [item for item in body["output"] if item.get("type") != "reasoning"]
        write(
            directory / "response.json",
            {
                "response": body,
                "latency_ms": latency,
                "request_id": getattr(response, "_request_id", None),
                "at": datetime.now(UTC).isoformat(),
            },
        )
        maximum, ceiling, output, rate, out_rate = LIMITS[payload["model"]]
        usage = response.usage
        if (
            not usage
            or response.model != payload["model"]
            or usage.input_tokens > ceiling + 1024
            or usage.output_tokens > output
        ):
            self.stop("Missing usage, model mismatch or token assumptions exceeded")
            raise GuardStop("Usage assumptions failed; raw response retained")
        # Upper rate estimate; cached tokens receive no discount. Cache write included.
        upper = (usage.input_tokens * rate + usage.output_tokens * out_rate) / Decimal(1000000)
        cached = usage.input_tokens_details.cached_tokens
        writes = usage.input_tokens_details.cache_write_tokens
        uncached = usage.input_tokens - cached - writes
        if min(cached, writes, uncached) < 0:
            self.stop("Inconsistent cache usage")
            raise GuardStop("Invalid cache accounting")
        standard = rate / Decimal("1.25")
        estimated = (
            uncached * standard
            + writes * rate
            + cached * standard / 10
            + usage.output_tokens * out_rate
        ) / Decimal(1000000)
        write(
            directory / "cost.json",
            {
                "upper_rate_estimate_usd": str(upper),
                "estimated_cost_usd": str(estimated),
                "reservation_usd": str(reserve),
                "usage": usage.model_dump(mode="json"),
                "billing_verified": False,
            },
        )
        return response, latency


def payload(model: str, instructions: str, content: str, schema: dict, name: str) -> dict:
    return {
        "model": model,
        "instructions": instructions,
        "input": [{"role": "user", "content": content}],
        "text": {"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
        "reasoning": {"effort": "none" if model.endswith("luna") else "high"},
        "max_output_tokens": LIMITS[model][2],
        "store": False,
        "tools": [],
        "tool_choice": "none",
        "truncation": "disabled",
        "service_tier": "default",
        "timeout": 60,
    }


class StudyProvider:
    def __init__(self, journal: Journal, prefix: str):
        self.journal, self.prefix, self.fixtures = journal, prefix, {}

    def __call__(self, request: dict) -> dict:
        key = self.prefix + "-" + request["agent"].split()[0].lower()
        response, _ = self.journal.response(
            key,
            payload(
                "gpt-5.6-luna",
                request["instructions"],
                request["input"],
                request["schema"],
                "RefundNodeResponse",
            ),
        )
        if response.status != "completed" or not response.output_text:
            raise GuardStop("Application response incomplete/refused")
        parsed = json.loads(response.output_text)
        self.fixtures[request_key(request)] = {"request": request, "response": parsed}
        return parsed

    def attribute(self, request):
        response, latency = self.journal.response(
            self.prefix + "-attribution",
            payload(
                request.model,
                request.instructions,
                request.evidence_json,
                AttributionDecision.model_json_schema(),
                "AttributionDecision",
            ),
        )
        parsed, usage = parse_structured_response(
            response, AttributionDecision, request.prompt_version, latency
        )
        return LLMResult(attribution=parsed, usage=usage)

    def generate(self, request):
        response, latency = self.journal.response(
            self.prefix + "-specification",
            payload(
                request.model,
                request.instructions,
                request.evidence_json,
                RegressionSpec.model_json_schema(),
                "RegressionSpec",
            ),
        )
        parsed, usage = parse_structured_response(
            response, RegressionSpec, request.prompt_version, latency
        )
        return RegressionResult(spec=parsed, usage=usage)


def open_client(key):
    return OpenAI(
        api_key=key.get_secret_value(),
        base_url="https://api.openai.com/v1",
        max_retries=0,
        timeout=60,
    )
