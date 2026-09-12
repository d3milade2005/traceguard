# Integration boundaries

These packages isolate trace formats, providers, and execution backends.

- `traces/`: `WhoWhenTraceAdapter` converts a single Algorithm-Generated or
  Hand-Crafted record into the shared `app.schemas.traces` Pydantic contracts.
  It accepts native-list or JSON-string history, preserves every message, and
  derives sequence exclusively from list position. It never reads benchmark labels.
  Persistence and source-byte preservation belong to the caller. Parquet loading
  and label interpretation belong to a future evaluation boundary. The small `TraceAdapter` protocol is consumed by submission and normalization
  services. `validate_submission` rejects benchmark fields in production; `adapt`
  remains usable on offline benchmark records and never reads their labels.
  `sources.py` composes the two supported Who&When variants.
- `llm/`: add a capability-oriented `LLMClient` protocol when attribution needs
  structured generation. Accept a Pydantic response model, return a validated
  instance, and translate SDK errors here. Inject clients into services so a
  Gemini implementation or test fake does not change attribution.
- `execution/`: add a `TestRunner` protocol with the validation phase. Services
  will consume execution results; this package will enforce isolation, limits,
  and cleanup. Do not execute generated Python in the API, worker, or host.

Use plain constructor injection. Do not create a plugin registry, provider factory
hierarchy, or speculative universal interface. Configuration and dependencies
arrive with the first consumer.
