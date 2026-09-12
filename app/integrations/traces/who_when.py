import json
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import JsonValue, ValidationError

from app.core.exceptions import TraceAdapterError
from app.schemas.traces import CanonicalTrace, TraceStep

WhoWhenVariant = Literal["algorithm_generated", "hand_crafted"]
ORCHESTRATOR_ROLE = re.compile(r"Orchestrator \(([^()]+)\)")


def _required_string(fields: Mapping[str, object], key: str, *, allow_empty: bool = False) -> str:
    value = fields.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise TraceAdapterError(f"Required field '{key}' must be a valid string.")
    return value


class WhoWhenTraceAdapter:
    """Normalize one record without consulting benchmark labels or changing the source."""

    def __init__(self, *, variant: WhoWhenVariant) -> None:
        if variant not in {"algorithm_generated", "hand_crafted"}:
            raise ValueError("Unsupported Who&When variant")
        self._variant = variant

    def adapt(self, record: Mapping[str, JsonValue]) -> CanonicalTrace:
        if not isinstance(record, Mapping):
            raise TraceAdapterError("The trace record must be an object.")
        task = _required_string(record, "question")
        history = record.get("history")
        if isinstance(history, str):
            try:
                history = json.loads(history)
            except (ValueError, RecursionError) as exc:
                raise TraceAdapterError("History must contain a JSON array of messages.") from exc
        if not isinstance(history, list) or not history:
            raise TraceAdapterError("History must be a nonempty array of messages.")

        try:
            steps = tuple(self._step(message, index) for index, message in enumerate(history))
            return CanonicalTrace(source="who_when", task=task, steps=steps)
        except ValidationError as exc:
            raise TraceAdapterError("Trace fields do not satisfy the canonical contract.") from exc

    def _step(self, message: object, sequence: int) -> TraceStep:
        if not isinstance(message, Mapping):
            raise TraceAdapterError("Every history entry must be a message object.")
        content = _required_string(message, "content", allow_empty=True)
        role = _required_string(message, "role")
        metadata: dict[str, JsonValue] = {"role": role}
        agent_id: str | None

        if self._variant == "algorithm_generated":
            agent_id = _required_string(message, "name")
        elif role == "human":
            agent_id = None
        elif match := ORCHESTRATOR_ROLE.fullmatch(role):
            agent_id = "Orchestrator"
            decorator = match.group(1)
            metadata["decorator"] = decorator
            if decorator.startswith("->"):
                recipient = decorator[2:].strip()
                if not recipient:
                    raise TraceAdapterError("A directed Orchestrator role requires a recipient.")
                metadata["recipient"] = recipient
        else:
            agent_id = role

        return TraceStep(
            sequence=sequence,
            agent_id=agent_id,
            event_type="message",
            output=content,
            step_metadata=metadata,
        )
