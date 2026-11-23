"""Explicit ingestion boundary for the developer-owned controlled execution."""

import json
from collections.abc import Mapping

from pydantic import JsonValue, ValidationError

from app.core.exceptions import TraceAdapterError
from app.schemas.traces import CanonicalTrace


class ControlledTraceAdapter:
    def validate_submission(self, record: Mapping[str, JsonValue]) -> None:
        self.adapt(record)

    def adapt(self, record: Mapping[str, JsonValue]) -> CanonicalTrace:
        try:
            trace = CanonicalTrace.model_validate_json(json.dumps(dict(record)))
            if trace.source not in {"controlled.calculator", "controlled.refund"}:
                raise ValueError("Unexpected controlled trace source")
            return trace
        except (ValueError, ValidationError) as exc:
            raise TraceAdapterError from exc
