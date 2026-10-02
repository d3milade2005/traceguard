# Phase 5: regression specification

Phase 5 ends at a validated, persisted data-only specification. It introduces no
compiler, generated Python, test execution, sandbox, frontend, or benchmark scoring.
No new dependencies are required.

## API and lifecycle

- `POST /api/v1/attributions/{attribution_id}/regression-specs`: requires a completed
  attribution; unfinished returns 409, unknown returns 404. A committed and published
  new operation returns 202 with a Location header; repeated POST returns 200 and the
  existing operation, including terminal failures. Publication failure returns 503
  with `error.regression_spec_id` and preserves the failed operation.
- `GET /api/v1/regression-specs/{regression_spec_id}`: returns lifecycle, disposition,
  IR, server-resolved scenario snapshot, safe failures, timestamps, and provider audit
  data. It does not expose claims, prompts, credentials, or private reasoning.

Operations follow `pending -> queued -> processing -> completed / failed` independently
of Run and Attribution. A completed operation's disposition is `synthesizable` or
`needs_review`. Review is a valid outcome, with a reason, null scenario/snapshot, and
empty assertions. It is not a failed provider request.

Abstention, truncated attribution evidence, unavailable decisive evidence, missing
target input, and unsafe scenario data decline before provider invocation. Other
unsupported setup or oracle requirements are explicitly delegated to the generator's
review disposition. Unsafe benchmark/credential-bearing structured evidence fails
closed instead of being sent to a provider. Oversized generation evidence fails with
`REGRESSION_INPUT_INVALID`; oversized generated scenarios fail validation.

## Contract consumed by Phase 6

The canonical contract is `app.schemas.regression_specs.RegressionSpec`. Phase 6 must
consume a completed `RegressionRecord` with `disposition="synthesizable"`, its validated
`spec`, and its `scenario_snapshot`. Review and failed records cannot be compiled.
The record's `attribution_id` supplies the parent linkage; citation sequence numbers
always belong to that attribution's Run. No further semantic LLM call is needed.

Every provider field is explicitly required; nullable fields use JSON null. Unknown
fields are forbidden at every nested level.

| IR field | Meaning |
| --- | --- |
| `version` | Literal `regression-spec-v1` |
| `disposition` | `synthesizable` or `needs_review` |
| `test_name` | `test_` plus a lowercase letter and at most 79 lowercase letters, digits, or underscores |
| `title`, `failure_summary` | Bounded human-readable intent, never executable semantics |
| `target_agent` | Exactly the attributed agent, or null for abstention |
| `scenario.stimulus` | `{sequence, field, path}` selecting complete decisive `input` or `tool_arguments`; path must be empty |
| `scenario.preconditions` | Up to 16 unique references to earlier supported canonical input/output/tool/status values |
| `assertions` | Up to 16 fixed-kind assertions; synthesis requires at least one |
| `evidence` | Up to 20 exact canonical citations supporting the diagnosed failure |
| `review_reason` | Required for review, null for synthesis |

Each assertion contains `kind`, `observable` (`output`, `tool_result`, or `status`),
`path`, `expected`, and nonempty `evidence`. A path is an ordered array of literal
object keys or nonnegative list indexes, at most 16 components. Keys match
`[A-Za-z_][A-Za-z0-9_-]*`; no dot paths, function calls, expressions, or dynamic lookup.
All assertions inspect the same target invocation that the scenario describes.

| Kind | Fixed semantics Phase 6 can map to a known template |
| --- | --- |
| `equals` | Present JSON scalar equals the typed expected scalar |
| `not_equals` | Present JSON value differs from the typed expected scalar |
| `contains` | Present string contains the nonempty expected string |
| `not_contains` | Present string omits the nonempty expected string |
| `exists` | Path is present; expected must be null |
| `not_exists` | Path is absent; expected must be null |

Equality is type-sensitive (booleans, integers, and floating-point numbers are distinct).
Missing operands fail comparison assertions, including negated comparisons. A present
null satisfies existence and can be compared with null. Containment on nonstring
observations is invalid. Comparisons do not accept generated objects, schemas, regex,
callbacks, module names, imports, or code. Scalar strings are limited to 4096 characters.

Each citation is `{sequence, field, quote}`. `field` is `task`, `input`, `output`,
`tool_arguments`, `tool_result`, or `status`. Quote is an exact nonblank substring
of that canonical field (nonstring fields use JSON serialization). Each assertion
must cite the decisive step, and comparison operands must appear in its citations.
The specification-level citations must include the decisive step.

`scenario_snapshot` is derived by the server, never generated: `{stimulus,
preconditions}` contains the selected complete JSON input and the ordered precondition
values. The corresponding IR references retain their original source fields and paths.
The compiler must serialize these as literal fixture data. A separately developer-owned
execution harness must map the target agent and these data to the real agent system;
the model cannot choose an entry point or executable setup. Harness wiring is Phase 6
work, not an implied runtime implementation here.

## Grounding and deterministic validation

Only AttributionEvidence sequences visible in the original AnalysisContext manifest
are offered to the generator. Complete canonical fields are projected, unrelated
metadata is omitted, and nested benchmark labels and credential keys are rejected.
Original trace bytes and canonical steps are never changed by synthesis. Trace prompt
injection remains user-message evidence; trusted instructions are separate.

Validation checks the completed attribution, agent agreement, canonical sequence
membership, quote validity, supported kinds and paths, scenario references and ordering,
data bounds, and forbidden executable-looking content. It rejects truncated evidence
for synthesis and requires at least one assertion violated by the decisive observation.
That check traverses JSON data; it does not execute an agent or generated test. Invalid
output is never repaired or retried for semantic reasons. Prose intent and the logical
sufficiency of a cited oracle still require semantic reasoning and may warrant review;
exact citation checks cannot prove every natural-language inference.

Code-looking strings are conservatively refused even as scenario fixture data. This
is a supported-domain restriction, not a general code detector or a future sandbox.
All values remain untrusted. Phases 6/7 must retain literal serialization and isolation.

## Provider, persistence, ownership, and recovery

`RegressionGenerator.generate(RegressionRequest) -> RegressionResult` is the focused
provider-independent boundary. `OpenAILLMClient` shares its Responses request/error
handling with attribution. Prompt version is `regression-spec-v1`. Defaults remain
`OPENAI_MODEL=gpt-5.6-luna`, `OPENAI_REASONING_EFFORT=medium`. Requests use strict
Structured Outputs, `store=False`, no tools, disabled truncation, bounded output, and
zero SDK retries. See the official [Structured Outputs documentation](https://developers.openai.com/api/docs/guides/structured-outputs).

Migration `0004` adds only `regression_specs`, with an Attribution foreign key and
unique Attribution ownership. Lifecycle, disposition, version, title, agent, reason,
provider configuration/usage, input digest, failure codes, claims, attempts, and times
are relational columns; IR and scenario snapshot are JSONB. Constraints enforce claim,
completion, failure, result, and bounded-attempt states; status/creation has an index.
Stop workers before downgrading to `0003`; only Phase 5 operations are removed.

The API commits the operation before publishing its ID. Workers acquire a PostgreSQL
row lock and expiring ownership token, load evidence, generate or decline, record usage,
validate, and atomically persist the final IR/snapshot/result columns. Duplicate active
deliveries cannot infer concurrently; terminal delivery is a no-op; stale completions
and usage writes are fenced. Celery tasks only compose and initiate this workflow.

Transient provider/database failures receive up to three delayed retries. Busy delivery
waits for claim expiry, up to six retries. Four durable claims bound crash recovery.
Permanent configuration, schema, refusal, output-limit, and semantic failures terminate
with safe failure codes. Usage is the latest returned attempt, not cumulative billing.
Provider uncertainty/crashes after a request can cause bounded repeat paid calls; this
does not promise exactly-once external inference. There is no outbox/recovery scheduler.
An operator can republish a stranded pending/queued/expired operation by the same ID:

```sh
uv run celery -A app.workers.celery_app:celery_app call traceguard.generate_regression_spec --kwargs '{"regression_spec_id":"<uuid>"}'
```

Configuration bounds are `REGRESSION_MAX_INPUT_BYTES=48000` and
`REGRESSION_MAX_SCENARIO_BYTES=16000`. Logs correlate run, attribution, operation, task,
provider/model/prompt, lifecycle stages, and safe usage without trace or prompt bodies.

## Verification and optional live smoke

Normal contract, grounding, SDK, worker, persistence, concurrency, migration, API, and
RabbitMQ smoke tests use fakes or an in-memory SDK HTTP transport. PostgreSQL integration
uses disposable unique schemas in a database ending `_test`. Configure
`TEST_DATABASE_URL` and `TEST_RABBITMQ_URL`, then run the normal suite, Ruff, and mypy.

The separate `tests/integration/test_live_regression.py` is opt-in. Supply
`RUN_LIVE_REGRESSION_TESTS=true`, `OPENAI_API_KEY`, `TEST_DATABASE_URL`, and
`LIVE_REGRESSION_ATTRIBUTION_ID`. The dedicated test database must be migrated to head
and contain one real completed, attributed result with full reconstructable input and
no prior regression operation. The test makes one request at most, performs no automatic
retry, validates and persists the result there, and checks usage. It accepts a generated
review result. Attribution creation is deliberately not another paid request in this test.

```powershell
$env:RUN_LIVE_REGRESSION_TESTS = "true"
$env:LIVE_REGRESSION_ATTRIBUTION_ID = "<completed-real-attribution-uuid>"
uv run pytest tests/integration/test_live_regression.py -q
Remove-Item Env:RUN_LIVE_REGRESSION_TESTS
Remove-Item Env:LIVE_REGRESSION_ATTRIBUTION_ID
```

## Implementation verification — 2026-10-02

The complete suite passed: **493 passed, 2 skipped**. PostgreSQL and RabbitMQ tests
ran against the existing local services, using a dedicated test database, disposable
schemas, and isolated test queues. Both opt-in live tests were skipped; no paid calls
were made. The API/worker smoke used fake generation. Upgrade/downgrade, uniqueness,
state constraints, foreign-key/schema consistency, and atomic failure behavior passed.
Ruff, formatting, mypy (67 application modules), `git diff --check`, and
`docker compose config --quiet` passed. The suite emitted one existing Starlette/AnyIO
deprecation warning.

New implementation files are the regression schema, input projection, semantic
validator, submission store, processing service, focused generator protocol, API router,
Celery task, migration `0004`, and five test modules. Existing application wiring,
OpenAI request handling, errors, logging, configuration examples, migration/task
expectations, and README were updated. Historical migrations, dependencies, and
`AGENTS.md` were unchanged. Updated application containers were not deployed; apply
the migration before starting the updated API and worker.
