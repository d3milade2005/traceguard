# Frontend/backend contract v1

The backend exposes `/api/v1`. The separate Vite branch owns `/`, `/login`,
`/docs/*`, `/dashboard`, `/runs`, `/runs/:runId`, and `/system`; no frontend code
is supplied here. Runtime schemas are available at `/openapi.json` and `/docs`.

All operational APIs require a GitHub-backed application session. Health, readiness,
OpenAPI, and OAuth initiation/callback remain public. This is a shared FYP workspace:
every authenticated user can access every run. Login does **not** provide per-run
ownership, tenant separation, organizations, teams, or RBAC.

## Authentication

| Method | Path | Authentication | Parameters | Success | Important errors |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/v1/auth/github` | Public | `next`, default `/dashboard`, safe relative ASCII path, up to 1024 characters | 302 to GitHub; sets a temporary HttpOnly browser-binding cookie | 400 unsafe next; 503 OAuth not configured |
| GET | `/api/v1/auth/github/callback` | Public, browser-binding cookie required | GitHub `code`, `state`; provider denial is rejected | 303 to configured frontend origin plus stored next; sets session cookie | 400 invalid/expired/reused state; 503 identity exchange failure |
| GET | `/api/v1/auth/me` | Required | None | 200 minimum profile below | 401 missing, expired, or revoked session |
| POST | `/api/v1/auth/logout` | Existing session optional; trusted Origin required | Empty body | 204; deletes server session and clears cookies; idempotent | 403 missing/untrusted Origin |

Start login with a **top-level browser navigation** to the backend `/auth/github` URL,
not a fetch that follows the redirect into GitHub. The backend uses authorization code,
single-use expiring state bound to the browser, and S256 PKCE. It requests no extra scopes:
only public identity is needed. GitHub's token is used server-side for `/user` and discarded;
it is never returned to React or stored in the session table. See
[GitHub's flow documentation](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps).

```json
{
  "id": "00000000-0000-0000-0000-000000000001",
  "github_id": 42,
  "username": "developer",
  "display_name": "Developer",
  "avatar_url": null,
  "created_at": "2026-10-17T12:00:00Z",
  "last_login_at": "2026-10-17T12:00:00Z"
}
```

The application session is an opaque random token. PostgreSQL stores only its SHA-256
digest, user ID, creation time, and expiry. Default lifetime is 12 hours, without sliding
renewal. A new login rotates the current browser session; logout invalidates it server-side.
OAuth state lasts 10 minutes. Concurrent callbacks cannot consume the same state twice.

Cookies are host-only, HttpOnly, Path `/`, SameSite=Lax. In local development they use
`traceguard_session` and `traceguard_oauth`, without Secure over localhost HTTP. Outside
development they are Secure with `__Host-` names. Do not read/store application tokens in
JavaScript or localStorage. Production frontend/backend must use HTTPS on the same site
(for example `ui.example.com` and `api.example.com`) or a same-origin reverse proxy;
unrelated-site deployments are not supported by these Lax cookies.

All fetches use `credentials: "include"`. POSTs must have an `Origin` exactly equal to
the configured frontend or backend origin; missing, `null`, wildcard, and other origins
are rejected. Browsers set this header. Trusted command-line clients must supply it.
CORS allows credentials only for explicitly configured origins. Authentication responses,
authenticated reads, and errors use `Cache-Control: no-store`.

## Runs ledger

`GET /api/v1/runs` requires authentication and returns 200.

| Parameter | Default | Constraint |
| --- | --- | --- |
| `limit` | 20 | 1–100 |
| `offset` | 0 | 0–100000 |
| `status` | none | pending, queued, processing, normalized, failed |
| `analysis_status` | none | active, completed, review_required, failed |
| `responsible_agent` | none | Exact persisted identity, 1–200 characters |
| `validation_verdict` | none | valid, not_reproduced, not_discriminating, inconclusive |
| `needs_attention` | none | true or false |

Unknown parameters and invalid values return 422; unauthenticated requests return 401.
Results sort by creation time descending, then UUID descending, for deterministic ties.
Offset pages and dashboard queries are live reads, not a frozen transactional snapshot
across separate HTTP requests.

```json
{
  "items": [{
    "id": "00000000-0000-0000-0000-000000000001",
    "source": "controlled.calculator",
    "status": "normalized",
    "created_at": "2026-10-17T12:00:00Z",
    "analysis_id": "00000000-0000-0000-0000-000000000002",
    "analysis_status": "completed",
    "analysis_stage": "validation",
    "analysis_stop_reason": "valid",
    "attribution_id": "00000000-0000-0000-0000-000000000003",
    "attribution_status": "completed",
    "responsible_agent": "calculator",
    "decisive_sequence": 1,
    "confidence": "high",
    "regression_spec_id": "00000000-0000-0000-0000-000000000004",
    "regression_disposition": "synthesizable",
    "generated_test_id": "00000000-0000-0000-0000-000000000005",
    "validation_id": "00000000-0000-0000-0000-000000000006",
    "validation_verdict": "valid",
    "needs_attention": false
  }],
  "total": 1,
  "limit": 20,
  "offset": 0
}
```

Stage fields may be null. Analysis-linked artifact IDs are authoritative. For standalone
stage requests, the projection selects the latest durable artifact using a deterministic
timestamp/UUID order. No trace bytes, context bodies, RegressionSpec bodies, or generated
Python are selected by the ledger query.

## Run detail and stage operations

The existing `GET /api/v1/runs/{runId}` is retained and enriched with the ledger fields.
It additionally returns `updated_at`, `failure_code`, `failed_at`, and
`normalized_step_count`. Use it as the detail header, then follow its stage IDs. No new
composite endpoint or duplicate persisted summary is introduced.

Every route below requires authentication. POST additionally requires a trusted Origin.
GET returns 200 or 404. Malformed UUIDs return 422. Existing stage contracts are preserved.

| Method | Path | Response |
| --- | --- | --- |
| POST | `/runs?source=controlled.calculator` | 202 `{ "id": "...", "status": "queued" }`; Location points to run |
| GET | `/runs/{runId}` | Run summary and stage IDs |
| POST | `/runs/{runId}/analysis` | 202 new or 200 existing `AnalysisRecord`; two inference stages at most |
| GET | `/runs/{runId}/analysis` | `AnalysisRecord`: status, stage, stop reason, deadline, stage IDs |
| POST | `/runs/{runId}/attributions` | 202 new or 200 existing attribution operation |
| GET | `/attributions/{attributionId}` | Decision, agent, decisive sequence, explanation, evidence, confidence, context manifest, usage, safe diagnostics |
| POST | `/attributions/{attributionId}/regression-specs` | 202 new or 200 existing specification operation |
| GET | `/regression-specs/{regressionSpecId}` | Disposition, structured spec, server-resolved scenario, usage, safe diagnostics |
| POST | `/regression-specs/{regressionSpecId}/generated-tests` | 201 new or 200 existing compiled artifact; unsupported replay fixture returns 422 with artifact ID |
| GET | `/generated-tests/{generatedTestId}` | Controlled generated source, digest, compiler/harness versions; treat source as text |
| POST | `/generated-tests/{generatedTestId}/validations` | 202 new or 200 existing validation operation |
| GET | `/validations/{validationId}` | Verdict, faulty/corrected outcomes, runtime identity, bounded execution summaries |

Paths in this table are relative to `/api/v1`. Submission requires JSON content type and
the complete supported trace object as body. It accepts failed traces, not Who&When labels.
Supported source strings are `who_when.algorithm_generated`, `who_when.hand_crafted`,
and `controlled.calculator`; the ledger preserves the submitted source. Failure before
prerequisites are ready returns 409;
unsupported data/harness returns 422; queue/dependency problems return 503. Original traces
are preserved before publication. Repeated stage POSTs reuse durable operations; a wrong
answer or terminal failure does not silently trigger another inference attempt.

Example analysis response (other metadata fields are documented in OpenAPI):

```json
{
  "status": "review_required",
  "stage": "compilation",
  "stop_reason": "REPLAY_CONTRACT_INCOMPATIBLE",
  "attribution_id": "00000000-0000-0000-0000-000000000003",
  "regression_spec_id": "00000000-0000-0000-0000-000000000004",
  "generated_test_id": "00000000-0000-0000-0000-000000000005",
  "validation_id": null
}
```

This is a field excerpt, not the complete AnalysisRecord. Never execute downloaded test
source in the browser, API process, or normal worker. Execution remains an explicit,
isolated validation operation. Poll reads; do not repeat inference POSTs as a polling strategy.

## Dashboard

`GET /api/v1/dashboard`, authenticated, no parameters, returns 200:

```json
{
  "total_runs": 0,
  "active_analyses": 0,
  "review_required_analyses": 0,
  "valid_regressions": 0,
  "recent_runs": [],
  "needs_attention_runs": []
}
```

The two lists contain at most five ledger items. Counts are simple PostgreSQL aggregates;
`valid_regressions` counts runs whose selected validation is valid, not all historical
artifacts. Needs attention includes normalization/stage failures, abstentions, review
dispositions, and non-valid validation verdicts. The existing analysis deadline is honored:
expired active analyses project as failed; the existing analysis GET/worker persists its
terminal transition. The read view does not create a second state store. Use Runs filters
to open the corresponding ledger. There are no chart or time-series APIs.

## System

`GET /api/v1/system`, authenticated, no parameters, returns 200:

```json
{
  "api_health": "ok",
  "environment": "development",
  "model": "gpt-5.6-luna",
  "reasoning_effort": "medium",
  "attribution_prompt_version": "attribution-v1",
  "context_policy_version": "phase3-structural-v1",
  "regression_spec_version": "regression-spec-v1",
  "regression_prompt_version": "regression-spec-v1",
  "compiler_version": "pytest-compiler-v1",
  "validator_version": "regression-validator-v1"
}
```

This reports API process/configuration information, not worker or Docker readiness.
`GET /health` returns `{ "status": "ok" }`; public `GET /ready` checks configured
database/broker availability and returns 200 or a safe 503. No endpoint enumerates
environment variables, credentials, Docker socket paths, or provider keys.

## Local integration and deployment

Use the same hostname consistently: frontend `http://localhost:5173`, backend
`http://localhost:8000`, OAuth callback
`http://localhost:8000/api/v1/auth/github/callback`. Do not mix localhost and 127.0.0.1
between browser-facing origins. Register a GitHub OAuth App, disable callback wildcard
matching, and put its ID/secret only in backend `.env`. Do not create a `VITE_` secret.

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Edit the existing .env if it already exists; do not replace real credentials.
uv sync --locked --group evaluation
docker compose up -d postgres rabbitmq
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

The example copy is for a new checkout only. The host DB URL uses published port 5433;
Compose application services override it to postgres:5432. If Windows Docker Desktop
port forwarding is unavailable, fix that environment condition or run the backend and
verification on the existing Compose network; do not change production database logic.
No paid work is enqueued by starting the API. In another terminal, start the existing
Linux workers when explicit analysis execution is wanted:

```powershell
docker compose up --build -d worker
docker compose --profile validation up --build -d validation-worker
```

Build the approved validation image first if missing, using the existing bundle-digest
process in [isolated validation](isolated-validation-v1.md). Workers do not need GitHub
OAuth secrets; the validation worker receives only explicit DB/broker/validation settings.
Starting workers may consume previously queued operations; check your durable queue/run
state before doing so. This backend pass did not start normal AI workers.

On the frontend branch, start its existing Vite workflow (`npm install`, then
`npm run dev -- --host localhost --port 5173`) and set its public API base to
`http://localhost:8000/api/v1`. No frontend files or package scripts are created here.

Production requires OAuth credentials, HTTPS frontend/callback origins, explicit CORS,
Secure host-only cookies, current migrations, protected PostgreSQL/broker, and the
existing restricted validation runner. Configure trusted reverse-proxy headers at deployment;
the OAuth callback/next destination use configured URLs, never an untrusted Host header.
API startup fails clearly if production OAuth is absent; workers do not require it.

## Errors

Errors retain the existing envelope and X-Request-ID correlation:

```json
{
  "error": {
    "code": "AUTHENTICATION_REQUIRED",
    "message": "Sign in to access this resource.",
    "request_id": "example-request"
  }
}
```

Do not display provider payloads or assume an inconclusive validation is valid. Future
structured-output failures expose only schema-owned paths, bounded error categories,
counts, digests, usage, and versions; rejected text and hidden reasoning are not retained.
