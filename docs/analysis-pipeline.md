# Backend/AI integration (before Phase 8)

`POST /api/v1/runs/{run_id}/analysis` starts analysis of an already normalized run.
It commits an `analyses` row before publishing a Celery coordinator task and returns
202 with a Location header. Repeated POST returns 200 and the same operation;
an active operation is requeued to recover an interrupted publication. Unknown
runs return 404; runs without complete normalization return 409.

`GET /api/v1/runs/{run_id}/analysis` returns the durable stage, status, stop reason,
model, reasoning effort, timestamps, and IDs for the existing stage endpoints:

| Stage | Result endpoint |
| --- | --- |
| Attribution | `/api/v1/attributions/{attribution_id}` |
| RegressionSpec | `/api/v1/regression-specs/{regression_spec_id}` |
| Compilation | `/api/v1/generated-tests/{generated_test_id}` |
| Validation | `/api/v1/validations/{validation_id}` |

The coordinator performs bounded, short polling tasks on the ordinary worker. It
submits through the existing services, serializes advancement on the analysis row,
and recovers stage IDs through those services' unique parent identities. Compilation
runs deterministically in that worker; validation is submitted to the existing
dedicated Docker worker. PostgreSQL remains authoritative. No new dependencies,
broker, result backend, frontend, or benchmark evaluation were introduced.

Only attribution and RegressionSpec synthesis call OpenAI. They use the model and
reasoning effort captured when analysis was requested (defaults: `gpt-5.6-luna`,
`medium`). Analysis-managed inference has **one claimed attempt per AI stage**,
including across expired leases and task redelivery. SDK retries remain disabled.
Thus a successful fresh pipeline makes two requests. An interrupted request can
have an uncertain provider outcome; it is never automatically repeated to obtain
a more convenient result. Existing standalone stage retry policies are unchanged
for runs without an analysis operation. Existing completed artifacts are reused.

| Condition | Analysis status / stop reason |
| --- | --- |
| Both executions discriminate correctly | `completed` / `valid` |
| Attribution abstains | `review_required` / `attribution_abstained` |
| Spec declines synthesis | `review_required` / `regression_needs_review` |
| Grounded fixture cannot be invoked by the approved harness | `review_required` / `REPLAY_CONTRACT_INCOMPATIBLE` |
| Other validation verdict | `review_required` / actual verdict |
| Provider, compilation, queue, or infrastructure failure | `failed` / safe error code |
| One-hour deadline expires | `failed` / `ANALYSIS_DEADLINE_EXCEEDED` |

The deadline is checked on advancement and reads, so a lost task does not appear
active indefinitely when inspected. If PostgreSQL itself is unavailable, no state
write can be guaranteed; the coordinator retries database connectivity within a
bounded task budget. A failed/review operation is terminal. Repeated POST does not
regenerate artifacts or spend additional inference calls.

## Controlled live demonstration

Apply migration head (`0008`), restart the API and ordinary worker, and start the dedicated
validation worker using the Phase 7 setup. Keep the pinned sandbox image available.
Configure `OPENAI_MODEL=gpt-5.6-luna` and `OPENAI_REASONING_EFFORT=medium` in the API
and ordinary worker. Set a sufficient provider timeout for the live run.

```powershell
uv run alembic upgrade head
uv run python -m scripts.run_live_analysis --cookie-file .local/session.cookies --live
# Resume the SAME operation if the client disconnects:
uv run python -m scripts.run_live_analysis --cookie-file .local/session.cookies --live --run-id <printed-run-id>
```

The authenticated script requires a Netscape-format cookie jar from a successful
GitHub browser login. Keep it private in ignored `.local`, never commit it. It sends
the configured frontend Origin and does not implement an authentication bypass.
See [frontend authentication](frontend-contract.md) for cookie/CORS setup.

The script runs developer-owned deterministic coordinator, calculator, and reporter
agents, detects their wrong final answer with an external oracle, and submits the
actual recorded trace through ingestion. These are deterministic agents, not LLM
agents; no model call occurs while creating their execution trace. The calculator
implementation is the same faulty implementation used by the approved sandbox
harness, whose corrected variant removes the off-by-one error. The generated test
replays the attributed calculator invocation and its recorded setup, rather than
replaying the whole workflow. Original bytes are preserved, and canonical sequences
remain unchanged. The adapter accepts only the explicit `controlled.calculator`
source and canonical envelope. Who&When is not used for replay or corrected fixtures.

The script saves all stage responses to `.local/final-e2e.json`, including provider
response IDs, model/usage metadata, artifact digest, isolated execution outcomes,
and verdict. It never executes generated Python on the host. A non-valid outcome
is reported as a failure, never patched into a successful result. Normal automated
tests use fakes and do not spend API credits.

## Live attempt, 2026-10-16

Run `03f6d89d-4bf0-4f78-8086-ce6ead190d93` used exactly one attribution request
and one synthesis request with `gpt-5.6-luna` / `medium`. Attribution correctly
identified `calculator`, canonical sequence 1, with high confidence. The synthesis
stage stopped with `REGRESSION_SPEC_INVALID` at citation-text validation. No generated
test or validation verdict exists for this live run. Its failed state and provider
response IDs remain persisted; `.local/final-e2e.json` contains the API report.

Inspection exposed inconsistent JSON rendering between provider input (compact)
and citation validation (spaced). Validation now accepts exact substrings of either
rendering of the same canonical value; fabricated values remain rejected. This is
a reproducible defect, but the exact offending live quotation cannot be confirmed:
the existing service did not retain the rejected response body and provider storage
was disabled. No response was fabricated or regenerated.

The diagnosis also omitted the successful coordinator setup from its citations.
Synthesis now retains earlier `setup` events only when they were fully visible in
the attribution context. Truncated/omitted setup stays excluded; attribution
citations remain unchanged. This prevents a successful setup event from becoming
unavailable merely because it did not cause the failure.

These fixes were tested offline before the separately authorized second live attempt
below. The first run remains failed and has not been rewritten.

Final offline verification passed 668 tests (two paid suites skipped), including
the coordinated controlled execution with real Docker and fake inference. A later
targeted pipeline run passed 15 tests, including preservation of stage links after
queue failure. Ruff, formatting checks on changed implementation files, mypy,
PostgreSQL migration/schema checks, and `git diff --check` passed. The existing
Starlette/AnyIO deprecation warning remains. Migration `0007` was applied locally.

## Successful live integration, 2026-10-16

The user authorized two additional calls for one fresh attempt. Run
`0f15b30d-68d8-4a8a-89a7-2cb3504dbf75` completed the entire pipeline through the API
and Celery workers, with persisted analysis status `completed` and validation verdict
`valid`. Both AI stages used `gpt-5.6-luna` with medium reasoning.

| Stage | Observed result |
| --- | --- |
| Controlled execution and ingestion | Actual coordinator/calculator/reporter trace; three normalized steps |
| Context | Complete trace, original sequences 0–2 |
| Real attribution | `calculator`, decisive sequence 1, high confidence |
| Real RegressionSpec | `synthesizable`; recorded input and coordinator setup; output `sum` must equal 4 |
| Deterministic compilation | `test_calculator_adds_two_operands` |
| Isolated faulty execution | `assertion_failed`, exit 1, expected regression assertion |
| Isolated corrected execution | `passed`, exit 0 |
| Persisted validation | `63e619c9-e8b4-465e-b227-c96b230029ff`, verdict `valid` |

The generated artifact and validation share SHA-256
`2881c056dc6a1d9124037b4c4197d154ff102e90cb00ce4d8082b7869e10a56f`.
Both variants ran with the pinned runtime image
`sha256:7a4b51cdd46e1e40dab633f0ad54c182c90ba7c9c4fef3f2ab6f6019d0f6bc32`.
Sandbox restrictions were unchanged; no generated Python executed on the host.

The successful run made one attribution request and one synthesis request, with no
provider retries. Across both explicitly authorized attempts, four OpenAI calls
were made in total. The earlier failure is preserved separately. Full successful
stage responses, provider IDs, usage, source, and sandbox results are saved in
`.local/final-e2e-attempt2.json`.

Final backend/AI integration is now verified for this controlled scenario. This is
not benchmark evaluation or evidence of general attribution accuracy. The separate
Phase 8 research tooling and measurements are documented in
[Evaluation and experiments](phase8-evaluation.md).
