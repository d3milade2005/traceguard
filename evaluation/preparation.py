"""Offline source preservation and request-level budgeting for attribution studies."""

import hashlib
import json
import platform
import subprocess
import sys
from decimal import Decimal
from importlib.metadata import distributions
from pathlib import Path

from app.schemas.attributions import AttributionDecision, AttributionRequest
from evaluation.artifacts import digest, timestamp, write_once

PRICING = {
    "version": "terra-standard-2026-11-23",
    "model": "gpt-5.6-terra",
    "verified_date": "2026-11-23",
    "source": "https://developers.openai.com/api/docs/models/gpt-5.6-terra",
    "service_tier": "default",
    "input_per_million_usd": "2.00",
    "cached_input_per_million_usd": "0.20",
    "cache_write_per_million_usd": "2.50",
    "output_per_million_usd": "12.00",
    "long_context_threshold": 272000,
    "long_input_multiplier": "2",
    "long_output_multiplier": "1.5",
    "billing_verified": False,
}


def request_body(request: AttributionRequest, *, max_output_tokens: int) -> dict:
    """Mirror the frozen Responses request body, excluding credentials and HTTP headers."""
    return {
        "model": request.model,
        "reasoning": {"effort": request.reasoning_effort},
        "instructions": request.instructions,
        "input": [{"role": "user", "content": request.evidence_json}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "AttributionDecision",
                "strict": True,
                "schema": AttributionDecision.model_json_schema(),
            }
        },
        "store": False,
        "tools": [],
        "tool_choice": "none",
        "service_tier": "default",
        "max_output_tokens": max_output_tokens,
        "truncation": "disabled",
    }


def cost_bound(body: dict, *, timeout_seconds: int) -> dict:
    if body["model"] != PRICING["model"]:
        raise ValueError("This pricing snapshot applies only to GPT-5.6 Terra")
    # Retain the historical conservative method, including extra local timeout bytes.
    size = len(json.dumps(body | {"timeout": timeout_seconds}, ensure_ascii=True).encode())
    input_bound = size + 4096
    input_price = Decimal(PRICING["cache_write_per_million_usd"])
    output_price = Decimal(PRICING["output_per_million_usd"])
    if input_bound > PRICING["long_context_threshold"]:
        input_price *= Decimal(PRICING["long_input_multiplier"])
        output_price *= Decimal(PRICING["long_output_multiplier"])
    cost = (input_bound * input_price + body["max_output_tokens"] * output_price) / 1000000
    return {
        "request_ascii_json_bytes": size,
        "input_token_planning_bound": input_bound,
        "max_output_tokens": body["max_output_tokens"],
        "planning_upper_cost_usd": str(cost),
        "method": "ASCII request bytes plus 4096 framing/schema tokens; no cache discount",
        "limitation": "Conservative planning estimate, not an invoice or enforced dollar limit",
    }


def snapshot_repository(root: Path, output: Path) -> dict:
    paths = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
        )
        .decode()
        .split("\0")
    )
    paths = sorted({p for p in paths if p and (root / p).is_file()} | {"AGENTS.md"})
    inventory = {}
    for relative in paths:
        original = root / relative
        if original.is_symlink():
            raise ValueError("Source snapshots require regular files")
        destination = output / "source" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw = original.read_bytes()
        with destination.open("xb") as stream:
            stream.write(raw)
        inventory[relative] = hashlib.sha256(raw).hexdigest()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    for filename, args in (
        ("git-status.txt", ["status", "--short"]),
        ("git-diff.patch", ["diff", "--binary", "HEAD"]),
    ):
        with (output / filename).open("xb") as stream:
            stream.write(subprocess.check_output(["git", *args], cwd=root))
    result = {
        "created_at": timestamp(),
        "base_commit": revision,
        "working_tree_sha256": digest(inventory),
        "files": inventory,
        "note": (
            "Byte hashes and source snapshot include uncommitted changes; no credentials copied"
        ),
    }
    write_once(output / "source-inventory.json", result)
    return result


def environment_identity() -> dict:
    return {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "packages": dict(
            sorted((d.metadata["Name"], d.version) for d in distributions() if d.metadata["Name"])
        ),
    }
