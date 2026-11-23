import json
from collections.abc import Mapping

from pydantic import JsonValue, ValidationError

from app.core.exceptions import TraceAdapterError
from app.schemas.langgraph import LangGraphTrace
from app.schemas.traces import CanonicalTrace, TraceStep


class LangGraphTraceAdapter:
    """Normalize captured events without importing LangGraph or executing uploaded code."""

    def parse(self, record: Mapping[str, JsonValue]) -> LangGraphTrace:
        try:
            return LangGraphTrace.model_validate_json(json.dumps(dict(record), allow_nan=False))
        except (ValueError, TypeError, ValidationError) as exc:
            raise TraceAdapterError from exc

    def validate_submission(self, record: Mapping[str, JsonValue]) -> None:
        self.parse(record)

    def adapt(self, record: Mapping[str, JsonValue]) -> CanonicalTrace:
        trace = self.parse(record)
        steps = []
        for sequence, event in enumerate(trace.events):
            metadata: dict[str, JsonValue] = {"execution_id": str(trace.execution_id)}
            if event.finished_at:
                metadata["finished_at"] = event.finished_at.isoformat()
            if event.error:
                metadata["error"] = dict(event.error)
            steps.append(
                TraceStep(
                    sequence=sequence,
                    agent_id=event.agent_name,
                    event_type=event.event_type,
                    input=event.input,
                    output=event.output
                    if event.output is not None or event.error is None
                    else {"error": dict(event.error)},
                    tool_name=event.tool_name,
                    tool_arguments=event.tool_arguments,
                    tool_result=event.tool_result,
                    status=event.status,
                    occurred_at=event.started_at,
                    step_metadata=metadata,
                )
            )
        return CanonicalTrace(source="langgraph", task=trace.task, steps=tuple(steps))
