import json
from collections.abc import Mapping
from typing import Protocol

from pydantic import JsonValue, TypeAdapter, ValidationError

from app.core.exceptions import TraceAdapterError
from app.schemas.traces import CanonicalTrace

_RECORD = TypeAdapter(dict[str, JsonValue])


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def decode_record(raw: bytes) -> dict[str, JsonValue]:
    """Decode a copy for inspection; callers retain the untouched original bytes."""
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        return _RECORD.validate_python(value, strict=True)
    except (ValueError, UnicodeError, RecursionError, ValidationError) as exc:
        raise TraceAdapterError("A UTF-8 JSON object is required.") from exc


class TraceAdapter(Protocol):
    def validate_submission(self, record: Mapping[str, JsonValue]) -> None:
        """Check the source's production envelope without deriving canonical steps."""
        ...

    def adapt(self, record: Mapping[str, JsonValue]) -> CanonicalTrace: ...
