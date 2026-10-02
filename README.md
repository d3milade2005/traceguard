# Traceguard

Backend foundation for post-mortem debugging of failed multi-agent executions.
The planned pipeline preserves a failed trace, normalizes it, attributes the
failure, constructs a RegressionSpec, compiles PyTest, and validates it in isolation.

**Current scope:** failed-trace submission, exact source-byte persistence, asynchronous
Who&When normalization, deterministic analysis context management, and explicitly
requested, structured OpenAI failure attribution. No generated Python is executed.

## Architecture

A modular monolith: FastAPI handles transport, services own workflows and
transactions, SQLAlchemy owns persistence, and integrations isolate external
systems. Thin Celery tasks call normalization and attribution services.

PostgreSQL is authoritative. RabbitMQ transports tasks; Celery results are disabled.
Submission commits the original trace before publishing an ID-only task and
returning HTTP 202. Publication failures preserve the Run and record a safe failure
code. Expiring PostgreSQL claims fence concurrent workers.

```text
app/
  main.py                    Application factory and lifespan
  api/                       HTTP errors, middleware, health, dependencies
    v1/                      Versioned route boundary
  core/                      Settings, application errors, JSON logging
  db/
    models/                  Run, TraceStep, Attribution, AttributionEvidence
    base.py, session.py      Metadata, engine and session lifecycle
  schemas/                   Validated contracts; no ORM responses
  services/                  Submission, normalization, context, persistence, readiness
  integrations/
    broker.py                AMQP readiness/error translation
    traces/                  Pure Who&When trace adapter
    llm/                     Responses API integration and LLMClient protocol
    execution/               Future isolated validation boundary
  workers/                   Celery configuration and correlation signals
alembic/versions/             Reviewed, explicit database migrations
tests/unit/                  No infrastructure or paid API calls
tests/integration/           Real PostgreSQL and RabbitMQ checks
.github/workflows/ci.yml      Lint, format, types, tests, Compose validation
```

The integration and worker READMEs explain where future functionality belongs.
Protocols arrive with their first real consumers; there is no plugin framework,
generic repository, or simulated processing task.

## Prerequisites and installation

- Python 3.12 (3.13 is also supported).
- [uv](https://docs.astral.sh/uv/getting-started/installation/).
- Docker with Compose v2 and Linux containers.

Run commands from the repository root:

For a complete host or Docker walkthrough that submits a trace, waits for
normalization, requests attribution, and reads the result, see
[E2E_RUNBOOK.md](E2E_RUNBOOK.md).

```sh
python -m pip install uv
uv sync --locked
cp .env.example .env
```

In PowerShell, `cp` is also available; alternatively use
`Copy-Item .env.example .env`. Do not overwrite an existing environment file.
The checked-in `uv.lock` fixes direct and transitive versions across supported
platforms. Use `uv lock --upgrade` only for a deliberate dependency update.

## Run everything with Docker

```sh
docker compose up -d --wait postgres rabbitmq
docker compose build
docker compose run --rm --no-deps backend alembic upgrade head
docker compose up -d backend worker
docker compose ps
docker compose logs -f backend worker
```

Migrations are an explicit step; application startup never creates or alters tables.
The shared application image runs as UID 10001. Infrastructure data uses named
volumes. Published ports bind to loopback. This Compose configuration is for local
development, with URL-safe development credentials; production deployment and
secret provisioning are separate work.

Stop services with `docker compose down`. Named volumes remain. If port 5432 is
already occupied, change the PostgreSQL host port in `docker-compose.yml` and the
matching port in your host `DATABASE_URL` before starting Compose.

## Run FastAPI on the host

```sh
docker compose up -d --wait postgres rabbitmq
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

In another terminal on Linux/macOS, start the worker:

```sh
uv run celery -A app.workers.celery_app:celery_app worker --loglevel=INFO
```

On Windows, use `docker compose up -d worker` for the Linux worker. Celery does not
officially support [Windows workers](https://docs.celeryq.dev/en/main/faq.html#windows). The host API is supported on Windows.

The worker registers `traceguard.process_run` and `traceguard.attribute_run`.
Normalization never triggers attribution automatically. To check its control connection:

```sh
docker compose exec worker celery -A app.workers.celery_app:celery_app inspect ping
```

## Endpoints

| Endpoint | Behavior |
| --- | --- |
| `GET /health` | HTTP 200 liveness; does not contact infrastructure |
| `GET /ready` | HTTP 200 after PostgreSQL query and authenticated RabbitMQ connection; HTTP 503 on dependency failure |
| `POST /api/v1/runs?source=...` | HTTP 202 after durable submission and task publication |
| `GET /api/v1/runs/{run_id}` | Processing state, timestamps, safe failure code, normalized step count |
| `POST /api/v1/runs/{run_id}/attributions` | Explicit attribution; HTTP 202 for a new queued operation, HTTP 200 for an existing operation |
| `GET /api/v1/attributions/{attribution_id}` | Attribution state, decision, evidence, usage, and safe audit metadata |
| `GET /docs` | Swagger UI |
| `GET /redoc` | ReDoc |
| `GET /openapi.json` | OpenAPI schema |

Readiness checks connectivity, not migration currency. Apply migrations separately.
A missing run returns the consistent `RUN_NOT_FOUND` HTTP 404 error.

API errors share this envelope:

```json
{"error":{"code":"DEPENDENCY_UNAVAILABLE","message":"A required service is unavailable.","request_id":"..."}}
```

Request IDs accept 1?64 ASCII letters, digits, underscores, or hyphens; invalid IDs
are replaced. JSON logs include request/task/run context where available.
Request bodies, query strings, and arbitrary extra log fields are excluded.
Exception logs retain type and stack locations without exception payloads or locals.
API responses never expose debug tracebacks, including in development.

## Configuration

Settings load from the environment, then `.env`; environment values win.
Unknown keys are ignored so Compose and optional test settings can share the file.
Database and broker URLs are required and masked in settings representations.

| Variable | Default / purpose |
| --- | --- |
| `APP_NAME` | `Traceguard` |
| `APP_ENV` | `development`; also `test`, `production` |
| `DEBUG` | `false`; rejected in production; never enables HTTP traceback pages |
| `API_V1_PREFIX` | `/api/v1` |
| `DATABASE_URL` | Required `postgresql+psycopg://...` |
| `RABBITMQ_URL` | Required `amqp://...` or `amqps://...` |
| `LOG_LEVEL` | `INFO` |
| `CORS_ALLOWED_ORIGINS` | JSON array, `[]`; explicit HTTP(S) origins only |
| `MAX_REQUEST_BODY_BYTES` | `10485760`; also bounds service trace persistence |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | `5`, `5`, per process |
| `DEPENDENCY_TIMEOUT_SECONDS` | `3`; connection/pool/query readiness limits |
| `WORKER_CONCURRENCY` | `2` |
| `NORMALIZATION_TIMEOUT_SECONDS` | `60`; soft limit; hard limit +10s and claim lease +30s |
| `ANALYSIS_CONTEXT__BUDGET` | `65536`; complete AnalysisContext JSON budget in UTF-8 bytes |
| `ANALYSIS_CONTEXT__RESERVED_HEADROOM` | `4096`; reserved for attribution request overhead; must be less than budget |
| `ANALYSIS_CONTEXT__HEAD_STEPS` | `2`; desired beginning window, at least 1 |
| `ANALYSIS_CONTEXT__TAIL_STEPS` | `4`; desired final window, at least 1 |
| `ANALYSIS_CONTEXT__NEIGHBOUR_STEPS` | `1`; neighbours outside each two-speaker transition, at least 1 |
| `OPENAI_API_KEY` | Optional secret; required only when a real attribution is processed |
| `OPENAI_MODEL` | `gpt-5.6-luna`; captured when attribution is submitted |
| `OPENAI_REASONING_EFFORT` | `medium`; captured when attribution is submitted |
| `OPENAI_TIMEOUT_SECONDS` | `60`; provider timeout; worker soft/hard limits +30/+40s, claim lease +60s |
| `OPENAI_MAX_OUTPUT_TOKENS` | `4096`; bounded output, including reasoning tokens |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Compose initialization; match host URL credentials |
| `RABBITMQ_DEFAULT_USER`, `RABBITMQ_DEFAULT_PASS`, `RABBITMQ_DEFAULT_VHOST` | Compose initialization; match host URL credentials |
| `TEST_DATABASE_URL`, `TEST_RABBITMQ_URL` | Optional integration-test connections |

Compose substitutes container hostnames while the example URLs address localhost.
Changing initialization credentials does not update already initialized volumes.
Application version comes from project package metadata. The API and offline tests
start without an OpenAI key. Sandbox settings remain deferred.

## Persistence and migrations

Migration `0001` creates only:

- **Run:** UUID, source, failure signal, exact original trace bytes (`BYTEA`),
  content type, request ID, timestamps, and constrained diagnostic state.
- **TraceStep:** UUID, run foreign key, unique nonnegative sequence per run,
  agent/event/tool/status fields, timezone-aware event time, and flexible JSONB
  input/output/tool payloads and metadata.

The source is inherited from Run. Accepted UTF-8 source bytes retain whitespace, field order, escapes, and other
details that JSONB would normalize. The HTTP boundary rejects duplicate JSON keys
and benchmark fields; lower-level persistence still preserves arbitrary bytes. Derived steps never
replace the original trace. Services expose no source-update operation.

Migration `0002` adds `updated_at`, normalized `task` context, claim token/expiry,
a persisted attempt count, and lifecycle constraints. States are `pending`,
`queued`, `processing`, `normalized`, and `failed`. These describe diagnostic
processing, not whether the submitted execution succeeded. A failed state requires
a safe code and timestamp. `normalized` does not imply attribution completed.

`RunService` receives a session factory. Writes use `with sessions.begin()` for
commit, rollback, and closure; reads close their sessions without committing.
Failure recording uses a fresh, conditional transaction and is repeatable.
SQL parameters are hidden. API lifespan disposes its engine; workers create
engines inside task execution, after forking.

Migration `0003` creates one unique Attribution per Run, with a separate lifecycle,
structured decision columns, ownership/attempt fields, provider usage, timestamps,
and safe audit metadata. AttributionEvidence rows contain inclusive canonical
sequence ranges, short reasons, ordering, and an explicit truncated-evidence flag.
The decisive sequence has a composite foreign key to the Run's canonical TraceStep.
Evidence content remains in TraceSteps. RegressionSpec and later tables are deferred.

```sh
uv run alembic upgrade head
uv run alembic current
uv run alembic check
uv run alembic revision --autogenerate -m "describe change"
```

Review generated migrations before applying them. To preview SQL without connecting:

```sh
uv run alembic upgrade head --sql
```

## Checks

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
docker compose config --quiet
```

Use `uv run ruff format .` to format. HTTPX2 provides the test client transport, as
recommended by [Starlette](https://www.starlette.io/testclient/). Unit tests cover configuration, lifecycle,
HTTP errors, correlation, payload limits, dependency translation, Celery setup,
and offline migration rendering. Integration tests skip with a reason unless their
URLs are configured; CI supplies both services.

For local integration checks using default Compose credentials:

```sh
docker compose exec postgres createdb -U traceguard traceguard_test
```

Uncomment `TEST_DATABASE_URL` and `TEST_RABBITMQ_URL` in `.env`, then run:

```sh
uv run pytest -m integration
```

The database name must end in `_test`. PostgreSQL tests migrate and remove only
their own random schema. They verify real BYTEA/JSONB persistence, constraints,
rollback, durable failure recording, migration upgrade/downgrade, and schema drift.
Worker tests publish ID-only tasks to their own RabbitMQ queues and check normalized
and fake-provider attribution results in PostgreSQL. Unit tests do not use infrastructure.

## Trace normalization

`app.schemas.traces.CanonicalTrace` holds `source`, task context, and an ordered
nonempty tuple of canonical `TraceStep` objects. Steps have nonnegative sequences,
optional agent identity and execution fields, and metadata. A complete canonical
trace requires consecutive zero-based sequences. Context selection will use a
separate representation so original positions remain stable.

```python
from app.integrations.traces.who_when import WhoWhenTraceAdapter

# record is one decoded JSON object or a Parquet-derived Python mapping.
trace = WhoWhenTraceAdapter(variant="algorithm_generated").adapt(record)
# Use variant="hand_crafted" for the other observed schema.
```

The adapter is pure and synchronous; it needs no database, worker, or Parquet
library. `history` may be a native list or a JSON-encoded list string. `question`
becomes task context. Every message, including empty bodies and human messages,
keeps its original position and text as `output`, with `event_type="message"`.

Algorithm-Generated uses `name` as agent identity. Hand-Crafted uses `role`, maps
`human` to no AI agent, and maps decorated Orchestrator roles to `Orchestrator`.
Metadata preserves `role`, plus `decorator` and directed `recipient` when present.
Missing inputs, status, timestamps, and tool fields stay `None`; code, OCR, and
terminal output embedded in message text are not interpreted.

Benchmark IDs, reference answers, correctness flags, mistake labels, and row
provenance are excluded. In particular, `mistake_step` is neither read nor validated
and never determines sequence. Its indexing semantics remain a future evaluation
question. Malformed trace structure raises `TraceAdapterError`; empty history is
rejected, while empty message content is retained. The adapter does not persist
or replace the source, and never copies unknown source fields into metadata.

## Analysis context management

`ContextManager` accepts a complete `CanonicalTrace` and returns a temporary
`AnalysisContext`. It has no database, HTTP, Celery, framework-adapter, or provider
dependencies. `RunService.canonical_trace()` loads and validates all persisted steps
in sequence order for a normalized run; it performs no writes and uses the stored
Run source identity. The existing worker still ends at `normalized`. There is no
context persistence, new processing state, or context API endpoint in this phase.

```python
from app.core.config import get_settings
from app.services.context import ContextManager

# runs is an existing RunService; run_id identifies a normalized persisted run.
trace = runs.canonical_trace(run_id)
context = ContextManager(get_settings().analysis_context).build(trace, run_id=run_id)
```

The contract contains optional run identity, source, the unchanged original task,
ordered selected steps, `full_trace_used`, inclusive `omitted_ranges`, and statistics.
Statistics record original/selected step counts, original/selected estimated sizes,
budget, reserved headroom, effective budget, size units, and pruning. Selected steps
retain their original sequences. For example, sequences `3, 4, 9, 10, 11` remain
those sequences; they are not assigned new positions. `SequenceRange(start=21,
end=24)` refers to canonical steps 21 through 24 inclusively. Gaps are explicit so
later evidence consumers cannot assume selected messages were adjacent.

The default `UTF8SizeEstimator` counts the **entire serialized AnalysisContext JSON**,
including task, identity, steps, omissions, excerpt metadata, and statistics. It is
a deterministic byte estimator, not a model token counter. It also counts JSON
escaping and multibyte Unicode. The default effective limit is 61,440 bytes after
4,096 bytes of headroom. A small injected `SizeEstimator` protocol permits a future
replacement; any replacement must be deterministic and monotonic, and its budget
must use its reported units. No provider or tokenizer dependency is introduced.

Selection follows a fixed structural policy:

1. Return every canonical step unchanged when the complete context fits.
2. Otherwise reserve the first and last steps, sharing available space if excerpts
   are necessary. For a one-step trace this is a single anchor.
3. Grow the beginning and final windows alternately, one adjacent whole step at a
   time, up to their configured sizes. Stop growing a side when its next step cannot
   fit; do not jump over that step.
4. Consider agent-identity changes from most recent to oldest. Each candidate
   includes both sides of the change and the configured outside neighbours. Add
   the entire window only if it fits, reusing overlap with already selected steps.

The strategy does not inspect message meaning, status, error text, or benchmark
labels to guess the faulty step. Desired windows are best effort under the budget.
Some capacity may remain unused when the next complete window cannot fit. There
is no guarantee that the future decisive failure step is selected.

Whole selected steps remain canonical `TraceStep` objects. An endpoint that cannot
fit with the other required evidence may instead become `TruncatedTraceStep`, with
`representation="serialized_excerpt"`, its original sequence/agent/event identity,
beginning/end fragments of its canonical JSON, and original/selected sizes. The
prefix gets the extra character for odd retained lengths. Binary search retains
the largest fragment pair that fits its allocation. Fragments may contain partial
JSON and must not be treated as a complete canonical payload or complete evidence.
This also handles oversized structured payloads and metadata without inventing
replacement JSON values. `full_trace_used` is false for any excerpt or omission.

The original task and minimum endpoint identities/envelope must fit. If they cannot,
the service raises `ContextBudgetError` (`CONTEXT_BUDGET_TOO_SMALL`) rather than
silently shortening the task or losing all evidence. Increasing the configured
budget is required for those inputs. Returned JSON payloads are deep copies: even
consumer mutations cannot modify the input canonical trace or ORM data. No raw
trace bytes, source steps, or stored sequences are replaced.

Unit tests cover full and bounded contexts, transition/endpoint windows, exact
serialized accounting, oversized steps, mutable JSON isolation, configuration,
contract invariants, and all committed Who&When fixture records. PostgreSQL
integration tests exercise persisted normalized traces through the read-only loader
and verify raw bytes, canonical steps, and Run state remain unchanged.

## Submit a failed trace

Create a UTF-8 file named `failed-trace.json` containing only production trace fields:

```json
{"question":"Original task","history":[{"name":"Agent","role":"assistant","content":""}]}
```

```sh
curl -i -X POST 'http://127.0.0.1:8000/api/v1/runs?source=who_when.algorithm_generated' -H 'Content-Type: application/json' -H 'X-Request-ID: example-1' --data-binary @failed-trace.json
```

On PowerShell, use `curl.exe`. `--data-binary` preserves the file bytes. The source
may also be `who_when.hand_crafted`, whose messages contain only `role` and `content`.
Both variants support native-list or JSON-string `history`. The top-level fields
are exactly `question` and `history`; benchmark fixture rows must have their labels
removed by the caller before submission. Labels are never accepted as query fields.
Empty content remains valid. Source structure is checked before submission; canonical
validation and normalization happen in the worker.

Response: HTTP 202, a `Location` header, and `{"id":"<uuid>","status":"queued"}`.
Poll that location:

```sh
curl http://127.0.0.1:8000/api/v1/runs/<uuid>
```

```json
{"id":"<uuid>","source":"who_when.algorithm_generated","status":"normalized","created_at":"<timestamp>","updated_at":"<timestamp>","failure_code":null,"failed_at":null,"normalized_step_count":1}
```

Submission asserts that the source execution has already failed. It records the
failure signal as `developer_report`; no semantic failure detection is performed.

## Processing and recovery

The workflow commits the original Run as `pending`, holds a short row lock during
confirmed publication, and records `queued`. On a known or uncertain publication
error it records `failed` / `QUEUE_PUBLICATION_FAILED` and returns HTTP 503 with
`error.run_id`; a possibly delivered task then observes the failure and does no work.

The worker commits a `processing` lease, adapts outside the transaction, then locks
and verifies its token before atomically inserting all steps and recording
`normalized`. Duplicate tasks cannot insert duplicate steps. Expired claims may be
reclaimed; older owners cannot persist results. Unexpired duplicate deliveries retry
later. Four persisted processing attempts bound repeated worker-crash redelivery.

Temporary database failures get at most three delayed Celery retries. Permanent input
failures are recorded without retry. Unexpected workflow errors are logged, recorded
with a safe code, and raised. Soft timeouts record failure where PostgreSQL is
available; hard process loss requeues the task and recovery waits for lease expiry.
Keep Compose shutdown grace above the configured hard timeout (default grace: 120s).

There is deliberately no outbox or automatic recovery scheduler. Process death
between the initial commit and publication may leave a visible `pending` Run.
Unavailable PostgreSQL during final error recording or a failed retry publication
may leave `queued`/`processing`; logs identify the unresolved outcome. After restoring
infrastructure, an operator can republish the same pending/queued/expired-processing
Run ID (terminal runs are harmless no-ops):

```sh
uv run celery -A app.workers.celery_app:celery_app call traceguard.process_run --kwargs '{"run_id":"<uuid>"}'
```

Stop workers before a migration downgrade. Downgrading `0002` preserves raw evidence
and steps but removes normalized task/claim fields and marks phase-2 states failed
with `SCHEMA_DOWNGRADED`. Restoring the schema does not resume those terminal runs.
HTTP submission itself is not deduplicated: a repeated POST creates a new Run.

## Explicit failure attribution

After a Run reaches `normalized`, request attribution explicitly:

```sh
curl -i -X POST http://127.0.0.1:8000/api/v1/runs/<run_uuid>/attributions
curl http://127.0.0.1:8000/api/v1/attributions/<attribution_uuid>
```

A new request returns HTTP 202 with `id`, `run_id`, `status="queued"`, and a
`Location` header. Repeated requests return the same operation with HTTP 200 and
its current status; they never enqueue another paid call. Unknown IDs return safe
404 errors; a non-normalized Run returns `RUN_NOT_NORMALIZED` / HTTP 409. Queue
failure preserves the Attribution, records `QUEUE_PUBLICATION_FAILED`, and returns
HTTP 503 with `error.run_id` and `error.attribution_id`.

The worker claims the Attribution, loads the canonical trace, uses the existing
ContextManager, constructs deterministic evidence input, and records its SHA-256
digest and safe visibility manifest before inference. The provider-independent
`LLMClient.attribute(request)` returns an `LLMResult` containing an
`AttributionDecision` and usage. Only the OpenAI integration imports the official SDK.
It uses Responses with a strict Pydantic-derived JSON schema, `store=False`, no
tools, no SDK retries, and no automatic provider-side input truncation.

Prompt `attribution-v1` treats the entire supplied trace as untrusted evidence.
It asks for a short grounded diagnosis, original canonical sequences, inclusive
evidence ranges, and abstention when visible evidence is insufficient. It requests
no chain-of-thought. Only task/source, visible steps, analytical metadata,
omissions, and explicit truncation representations enter the evidence document.
Benchmark labels never enter from Who&When records. Structured label/credential
fields in selected canonical payloads are rejected before any provider call;
unrelated metadata is omitted. Partial JSON excerpts with unknown metadata fail
closed because that metadata cannot be reliably filtered. Such inputs need a
larger context budget or supported analytical metadata. This is not a general
secret detector: developers must avoid putting credentials in free-text traces.

`decision="attributed"` requires a visible agent, a visible decisive canonical
sequence, and evidence covering that sequence. Every sequence in each ordered,
non-overlapping range must exist and have been visible. Invalid references are
rejected without repair. Truncated visible steps can be cited and remain marked.
`decision="abstain"` requires null agent, sequence, and category plus a concise
reason; it completes successfully. Category is optional, with no imposed taxonomy.
The explanation is a diagnosis, not a private reasoning transcript.

The Attribution lifecycle is `pending → queued → processing → completed / failed`;
Run remains `normalized`. Result and evidence commit together. Unique Run ownership,
row locks, expiring claim tokens, terminal no-ops, and evidence uniqueness fence
duplicates and stale workers. Transient network, timeout, rate-limit, provider 5xx,
and database failures get up to three delayed Celery retries (2/4/8s); four durable
claims bound crash redelivery. Busy duplicates wait for lease expiry, up to six
retries. Configuration/authentication/quota, refusal, incomplete/output-limit,
schema, and semantic failures are safe terminal failures, never forced guesses.

GET includes configured and response model, prompt version, response ID, input,
output and reasoning tokens, latency, timestamps, failure code, input digest, and
a visibility/truncation/omission manifest. It exposes no prompts, trace bodies,
ownership tokens, API keys, or reasoning transcripts. Usage describes the latest
returned attempt, not cumulative billing. A crash or uncertain timeout after a paid
request can cause a bounded repeat call; PostgreSQL guarantees one persisted result,
not exactly-once execution at an external provider. Context policy is read by the
worker; avoid changing it during retries if identical input is required.

There is no outbox or recovery scheduler. An operator may republish a stranded
pending/queued/expired operation using its original ID:

```sh
uv run celery -A app.workers.celery_app:celery_app call traceguard.attribute_run --kwargs '{"attribution_id":"<uuid>"}'
```

Terminal operations are no-ops; repeated POST does not reset failed operations.
Stop workers before downgrading `0003`; downgrade removes attribution results and
evidence while preserving normalized Runs, raw traces, and TraceSteps.

## Optional one-request live test

Normal tests use FakeLLMClient or the official SDK with an in-memory HTTP transport.
No paid call runs without explicit opt-in. Set an OpenAI key through the environment
or your uncommitted `.env`, configure `TEST_DATABASE_URL`, then run:

```sh
RUN_LIVE_LLM_TESTS=true uv run pytest tests/integration/test_live_attribution.py -q
```

PowerShell:

```powershell
$env:RUN_LIVE_LLM_TESTS = "true"
uv run pytest tests/integration/test_live_attribution.py -q
Remove-Item Env:RUN_LIVE_LLM_TESTS
```

This makes one request with `gpt-5.6-luna` / `medium` against the smallest committed
Algorithm-Generated fixture record, stripped of evaluation fields. It verifies
structured output, semantic references, persistence, and usage. It does not score
accuracy or retry the request. Missing key or opt-in skips the test.

## Regression specification (Phase 5)

Explicitly request `POST /api/v1/attributions/{attribution_id}/regression-specs`, then
read the Location URL with `GET /api/v1/regression-specs/{regression_spec_id}`.
Only completed attributions are accepted. New published operations return 202;
repeated requests return the existing operation with 200 and do not generate again.
Attribution abstention and unreconstructable inputs complete as `needs_review`
without calling OpenAI. A failed publication returns 503 with the preserved operation ID.

Phase 5 persists a bounded data-only `regression-spec-v1` IR and canonical scenario
snapshot. Six assertion kinds cover scalar equality/inequality, string containment,
and path presence/absence. Quotes and canonical references are validated, and at least
one assertion must catch the observed failure. PostgreSQL claims fence duplicates and
stale workers. Provider settings, retry classification, usage capture, and structured
logging follow attribution. Generation remains explicitly triggered; it never starts
automatically after attribution.

Run `uv run alembic upgrade head` before starting the updated API/worker. Migration
`0004` adds only regression specifications; historical migrations remain unchanged.
See [the Phase 5 contract and operation guide](docs/regression-spec-v1.md) for the exact
Phase 6 input, DSL semantics, review behavior, configuration, recovery, limits, and
optional one-request live test.

## Deterministic PyTest compilation (Phase 6)

Request `POST /api/v1/regression-specs/{regression_spec_id}/generated-tests` to compile a
completed synthesizable specification and its server-resolved snapshot synchronously.
A new artifact returns 201; repeated requests return the existing artifact with 200.
Read source, digest, compiler/harness versions, lifecycle, and safe failure information
with `GET /api/v1/generated-tests/{generated_test_id}`.

The pure compiler renders literal JSON and six fixed assertion helpers against an
approved developer-owned harness protocol. It parses and statically checks source,
then persists the artifact atomically. It never calls an LLM or runs/imports generated
code. Who&When review outcomes remain valid and cannot be compiled. Migration `0005`
adds the artifact table; run `uv run alembic upgrade head` before starting the updated API.
See [the compiler and Phase 7 contract](docs/pytest-compiler-v1.md) for exact semantics,
source validation, API errors, limits, idempotency, and the required isolated harness.

## Next phase

Phase 7 can execute completed GeneratedTest artifacts with an approved harness in an
isolated bounded environment and compare faulty/corrected behavior. Isolated execution,
validation-result persistence, benchmark scoring, model comparison, authentication,
and frontend work remain deferred.
