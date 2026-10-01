# Run Traceguard end to end

This guide runs the real FastAPI application, PostgreSQL, RabbitMQ, and Celery
worker through trace ingestion, normalization, an explicitly requested attribution,
and result retrieval. Attribution calls OpenAI and uses API credits. The fake
provider smoke test near the end exercises the same API/queue/database path without
a paid call.

Run commands from the repository root. You need Docker Compose v2, Python 3.12 or
3.13, and `uv`. On Windows, use PowerShell and Docker for the Celery worker.

## Configure the environment

Create `.env` only if you do not already have one; this preserves an existing API
key and local settings:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

The example file has local PostgreSQL and RabbitMQ credentials that work with
Compose. Check that `DATABASE_URL`, `RABBITMQ_URL`, `POSTGRES_*`, and
`RABBITMQ_DEFAULT_*` use the same credentials. Set `OPENAI_API_KEY` in `.env` for
the real attribution step. Keep `.env` private; it is ignored by Git.

If Docker cannot bind localhost ports `5432` or `5672`, change the host-side
published ports in `docker-compose.yml` and update the matching ports in
`DATABASE_URL` and `RABBITMQ_URL` for host-run services. Container-to-container
URLs remain on ports `5432` and `5672`.

Install the locked Python dependencies if you want to run the API on the host:

```powershell
uv sync --locked
```

## Option A: run the entire app in Docker

Build the application image, start the dependencies, apply migrations, and start
the API and worker:

```powershell
docker compose build
docker compose up -d --wait postgres rabbitmq
docker compose run --rm --no-deps backend alembic upgrade head
docker compose up -d --wait backend worker
```

Check that both endpoints respond:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
```

`/ready` checks PostgreSQL and RabbitMQ connectivity. Migrations are a separate
step, so run the migration command after a fresh database or schema update.

## Option B: run the API on your host

Start PostgreSQL and RabbitMQ in Docker, then migrate the database:

```powershell
docker compose up -d --wait postgres rabbitmq
uv run alembic upgrade head
```

In one terminal, run FastAPI:

```powershell
uv run uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

In another terminal, start the worker. Windows Celery workers are not supported,
so run the worker in Docker:

```powershell
docker compose up -d worker
```

The worker uses Compose's internal PostgreSQL/RabbitMQ addresses; the host API
uses the localhost URLs in `.env`. On Linux or macOS, you can instead run the
worker on the host in a third terminal:

```sh
uv run celery -A app.workers.celery_app:celery_app worker --loglevel=INFO
```

## Submit a trace and request attribution

The API accepts a Who&When-shaped record containing only `question` and `history`.
The example below creates an intentionally incorrect arithmetic execution. These
are ordinary trace contents, not benchmark labels. Save the returned IDs: you will
use them to follow the asynchronous work.

In PowerShell, submit the trace and keep the returned Run ID:

```powershell
$trace = @{
    question = "What is 2 + 2? Return exactly 4."
    history = @(
        @{ name = "ArithmeticAgent"; role = "assistant"; content = "The answer is 5." }
        @{ name = "ReviewAgent"; role = "assistant"; content = "The submitted answer is 5, but the task asks for 4." }
    )
} | ConvertTo-Json -Depth 10 -Compress

$run = Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:8000/api/v1/runs?source=who_when.algorithm_generated" `
    -ContentType "application/json" `
    -Body $trace
$run
```

Submission returns `202 Accepted` with an ID and `queued` status. Poll until
normalization finishes:

```powershell
$runStatus = $null
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    $runStatus = Invoke-RestMethod "http://127.0.0.1:8000/api/v1/runs/$($run.id)"
    if ($runStatus.status -in @("normalized", "failed")) { break }
    Start-Sleep -Seconds 1
}
$runStatus
if ($runStatus.status -ne "normalized") { throw "Normalization failed; inspect failure_code and worker logs." }
```

Normalization does not call the model. Make one paid attribution request explicitly:

```powershell
$attribution = Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:8000/api/v1/runs/$($run.id)/attributions"
$attribution
```

Poll the returned attribution ID until it is `completed` or `failed`:

```powershell
$result = $null
for ($attempt = 0; $attempt -lt 120; $attempt++) {
    $result = Invoke-RestMethod "http://127.0.0.1:8000/api/v1/attributions/$($attribution.id)"
    if ($result.status -in @("completed", "failed")) { break }
    Start-Sleep -Seconds 1
}
$result | ConvertTo-Json -Depth 10
if ($result.status -ne "completed") { throw "Attribution failed; inspect failure_code and worker logs." }
```

The completed response includes the decision, explanation, canonical decisive
sequence, evidence ranges and reasons, abstention reason when applicable, model,
token usage, and timestamps. An abstention is a successful completed decision.
Repeated POSTs for this Run return its existing Attribution and do not queue a
second provider call.

## Run the end-to-end smoke test without OpenAI

The integration smoke test runs FastAPI's test client and a real Celery worker
against PostgreSQL and RabbitMQ. It injects `FakeLLMClient`, so it verifies the
full queue and persistence path without using API credits. It creates an isolated
PostgreSQL schema and removes that schema after the test.

Add these two values to `.env`. The test database must be dedicated and its name
must end in `_test`:

```dotenv
TEST_DATABASE_URL=postgresql+psycopg://traceguard:traceguard_dev@localhost:5432/traceguard_test
TEST_RABBITMQ_URL=amqp://traceguard:traceguard_dev@localhost:5672/traceguard
```

Start PostgreSQL and RabbitMQ. Create the test database once if it does not exist:

```powershell
docker compose up -d --wait postgres rabbitmq
docker compose exec postgres createdb -U traceguard -O traceguard traceguard_test
```

Then run the end-to-end smoke test:

```powershell
uv run pytest tests/integration/test_attributions.py::test_real_api_and_celery_rabbitmq_worker_with_fake_llm -m integration -q
```

The test submits a Run through the API, normalizes it in a real Celery worker,
requests attribution through the API, and retrieves the persisted decision. It
uses `FakeLLMClient`, so it makes no OpenAI call. It creates and removes its own
random PostgreSQL schema; the dedicated test database remains. Never point
integration tests at a database containing data you need. If `traceguard_test`
already exists, skip the `createdb` command only if that database is dedicated to
tests.

For a single real model request against one small committed trace, see the opt-in
instructions in the README section “Optional one-request live test.” It requires
`RUN_LIVE_LLM_TESTS=true` and a configured API key.

## Inspect logs and stop the app

Follow API and worker logs while tasks run:

```powershell
docker compose logs -f backend worker
```

Stop the containers when finished:

```powershell
docker compose down
```

This keeps the PostgreSQL and RabbitMQ volumes, so Runs and attributions remain
available after the containers stop. `docker compose down -v` also deletes those
volumes and all data stored in them.
