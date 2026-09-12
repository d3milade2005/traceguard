# Traceguard

Backend foundation for post-mortem debugging of failed multi-agent executions.
The planned pipeline preserves a failed trace, normalizes it, attributes the
failure, constructs a RegressionSpec, compiles PyTest, and validates it in isolation.

**Current scope:** failed-trace submission, exact source-byte persistence, asynchronous
Who&When normalization, and retrieval of durable processing state. No generated
Python is executed.

## Architecture

A modular monolith: FastAPI handles transport, services own workflows and
transactions, SQLAlchemy owns persistence, and integrations isolate external
systems. Celery workers call the normalization service through a thin task function.

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
    models/                  Run and TraceStep
    base.py, session.py      Metadata, engine and session lifecycle
  schemas/                   Validated contracts; no ORM responses
  services/                  Submission, normalization, persistence, readiness
  integrations/
    broker.py                AMQP readiness/error translation
    traces/                  Pure Who&When trace adapter
    llm/, execution/         Future integration boundaries
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

The worker registers `traceguard.process_run`. To check its control connection:

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
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Compose initialization; match host URL credentials |
| `RABBITMQ_DEFAULT_USER`, `RABBITMQ_DEFAULT_PASS`, `RABBITMQ_DEFAULT_VHOST` | Compose initialization; match host URL credentials |
| `TEST_DATABASE_URL`, `TEST_RABBITMQ_URL` | Optional integration-test connections |

Compose substitutes container hostnames while the example URLs address localhost.
Changing initialization credentials does not update already initialized volumes.
Application version comes from project package metadata. LLM and sandbox settings
will be added when used.

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

Attribution, AttributionEvidence, RegressionSpec, GeneratedTest, and ValidationResult
are deferred until their contracts and cardinalities can be validated.

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
The live worker test publishes an ID-only task to its own RabbitMQ queue and checks
the normalized result in PostgreSQL. Unit tests do not use infrastructure.

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
Keep Compose shutdown grace above the configured hard timeout (default grace: 90s).

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

## Next phase

Define the non-destructive analysis-context contract and evidence references before
attribution. Keep canonical step positions and original evidence unchanged.

Context selection, LLM providers, attribution, RegressionSpec, PyTest compilation,
isolated validation, benchmark evaluation, authentication, and frontend work remain
deferred.
