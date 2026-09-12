from app.integrations.traces.contracts import TraceAdapter
from app.integrations.traces.who_when import WhoWhenTraceAdapter


def trace_adapters() -> dict[str, TraceAdapter]:
    return {
        "who_when.algorithm_generated": WhoWhenTraceAdapter(variant="algorithm_generated"),
        "who_when.hand_crafted": WhoWhenTraceAdapter(variant="hand_crafted"),
    }
