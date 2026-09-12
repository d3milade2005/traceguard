# Integration boundaries

These packages reserve three required boundaries, without simulated implementations.

- `traces/`: add the canonical Pydantic contract and a small `TraceAdapter` protocol
  with the first actual source adapter. Parsing produces derived steps and never
  updates `Run.original_trace`. Benchmark labels belong outside production models.
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
