# Traceguard

Backend foundation for post-mortem debugging of failed multi-agent executions.
The planned pipeline preserves a failed trace, normalizes it, attributes the
failure, constructs a RegressionSpec, compiles PyTest, and validates it in isolation.

**Current scope:** configuration, HTTP infrastructure, PostgreSQL persistence,
migrations, worker infrastructure, canonical trace contracts, and Who&When
normalization. There is no trace-submission API or processing
pipeline yet. No generated Python is executed.

## Architecture

A modular monolith: FastAPI handles transport, services own workflows and
transactions, SQLAlchemy owns persistence, and integrations isolate external
systems. Celery workers will call services through thin task functions.

PostgreSQL is authoritative. RabbitMQ transports tasks; Celery results are disabled.
A future submission must commit the original trace before publishing a task and
returning HTTP 202. Queue-handoff failures and worker recovery need explicit handling
when processing is implemented.

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
  services/                  Run persistence and readiness
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

The worker has no application tasks yet. To check its control connection:

```sh
docker compose exec worker celery -A app.workers.celery_app:celery_app inspect ping
```

## Endpoints

| Endpoint | Behavior |
| --- | --- |
| `GET /health` | HTTP 200 liveness; does not contact infrastructure |
| `GET /ready` | HTTP 200 after PostgreSQL query and authenticated RabbitMQ connection; HTTP 503 on dependency failure |
| `GET /docs` | Swagger UI |
| `GET /redoc` | ReDoc |
| `GET /openapi.json` | OpenAPI schema |

Readiness checks connectivity, not migration currency. Apply migrations separately.
No business endpoints are mounted under `/api/v1` yet.

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

The source is inherited from Run. Source bytes retain whitespace, duplicate JSON
keys, encoding, and other details that JSONB would normalize. Derived steps never
replace the original trace. Services expose no source-update operation.

`pending` means persisted diagnostic input; `failed` means a diagnostic failure
was recorded. These states do not describe whether the source execution succeeded.
A failed state requires a safe code and timestamp. Queued/processing/completed/review
states will arrive with the actual processing workflow.

`RunService` receives a session factory. Writes use `with sessions.begin()` for
commit, rollback, and closure; reads close their sessions without committing.
Failure recording uses a fresh, conditional transaction and is repeatable.
SQL parameters are hidden. API lifespan disposes its engine; future workers must
create engines after forking.

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
RabbitMQ tests authenticate without publishing application tasks.

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

## Next phase

Implement trace submission using the canonical contracts and Who&When adapter:
byte-preserving ingestion, persist-before-publish behavior, durable
processing transitions, and an idempotent thin worker task. Add end-to-end tests
for queue-handoff failure and worker retry/recovery before attribution work.

Context selection, LLM providers, attribution, RegressionSpec, PyTest compilation,
isolated validation, benchmark evaluation, authentication, and frontend work remain
deferred.
