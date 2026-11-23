# Live experiment preparation and regular-stack verification

**21 October 2026, Africa/Lagos. Status: ready for paid-call approval; zero model calls made.**

The regular backend and worker were rebuilt and recreated from current V1 source. The API at
port 8000, existing PostgreSQL, RabbitMQ, normal Celery queue and frontend at port 5173 now
work together. The earlier temporary port-8011 API and injected attribution fixture are not used.

| Verification | Observed result |
| --- | --- |
| Backend image | `sha256:7d7f1eec6f8c70dcc531d9a90860a8bc7226d143ebd03f704288da44f24a6435` |
| Worker image | `sha256:261f369fff29dd1ba3ff9c479bc2ddb52070dffd9266bb3c14a4d1ed556a7433` |
| `/health`, `/ready` | Both HTTP 200; readiness checks PostgreSQL and RabbitMQ |
| Normal Celery worker | Broker-connected `inspect ping` returned `pong`; worker container healthy |
| PostgreSQL / RabbitMQ | Existing containers healthy; volumes retained |
| Database migration | `0008 (head)`; no migration changes |
| Registry in rebuilt API | Includes `langgraph` and all existing sources |
| Model settings in API and worker | `gpt-5.6-terra`, effort `high`, output limit 8,192, timeout 180 seconds |
| Paid inference while waiting | Explicitly disabled with blank-key Compose overrides; stored key unchanged |
| Focused example/helper unit tests | **17 passed**, 5.41 seconds; provider boundary mocked |
| Frontend typecheck / production build | Both passed; 1,644 modules, 7.01-second final build |
| Focused Ruff lint / formatting | Passed; five Python files already formatted |
| Protected research/evaluation files | **26/26 unchanged** against pre-integration byte hashes |

## Actual ingestion preflight

[Open the ingestion preflight Run](http://localhost:5173/runs/057abb02-a10e-56c8-baa4-b22eea316232).

This Run is **deterministic graph ingestion only**, not the forthcoming live experiment.
No attribution fixture or model provider was called, and no Analysis was requested.

The actual refund example executed through LangGraph and submitted over authenticated HTTP to
the normal API. The normal Docker worker consumed `traceguard` and normalized the trace into
five canonical events. Original payload equality, real agent identities, sequence order and
duplicate identity reuse were verified. A subsequent Analysis lookup returned 404, confirming
that this preflight had not requested analysis. The reported failure remains the independent
refund-policy evaluator signal.

Actual browser checks at **1440px and 390px** displayed the new Run, five events, Eligibility
Decision Agent at sequence 3, its actual approval output, and the explicit “No analysis has been
requested” message. The existing System page displayed Terra/high configuration. There were no
JavaScript page errors or horizontal overflow in the trace view. Configuration display is not
evidence that Terra inference succeeded.

GitHub login was initially selected, configured locally and its OAuth start redirect verified.
The user then removed OAuth from this experiment's requirements. The final workflow retains
existing local dashboard access and uses a short-lived local verification session for the HTTP
helper. Cookie/Origin checks still apply. No claim of a genuine GitHub identity exchange is made.
The temporary session is in an ignored local file and must be removed after live verification.

## Essential preparation changes

No backend production logic, schema, scoring or frozen evaluation files were changed during this
preparation. Changes beyond V1 are limited to:

- `examples/langgraph_refund/live_agents.py`: explicit official API destination, effort `none`
  for the three Luna agents, and per-response usage/latency recording outside graph state.
- `examples/langgraph_refund/run.py`: save actual agent call count and usage alongside evidence.
- `tests/unit/test_langgraph.py`: verify those request settings and usage capture using a fake
  SDK only. The test creates no provider requests.
- Separate frontend `src/api/client.ts`: include available session cookies on API requests.
  The prior V1 trace error-style fix remains. Experimental frontend OAuth-control changes were
  reverted when the user chose to retain local development access; no redesign was made.
- Ignored local Compose overrides: requested Terra High settings and temporary inference disable.

## Evidence and next step

All new local preflight evidence is under `.local/langgraph-live-v1/`:

- `runtime-preflight.json`: aggregate settings, image IDs, health, checks and protected hashes.
- `ingestion-preflight.json`, `preflight-execution.json`: actual Run identity and graph evidence.
- `preflight-unit-tests.xml`: focused test results.
- `ui-preflight.json`: actual browser assertions and viewport records.
- `preflight-trace-{1440,390}.png`: actual trace screenshots.
- `preflight-system-{1440,390}.png`: current model-configuration screenshots.

These are preflight artifacts and must not be presented as screenshots/results of paid inference.
The [live execution plan](langgraph-live-v1-plan.md) specifies the exact four-request budget,
models, costs, limits and honest external replay outcome. After explicit approval, execute one
new LLM-driven graph, use real Terra High attribution, record the actual result and usage, and
capture fresh screenshots for the thesis report. The live Run ID and attribution correctness
remain unknown until that experiment is performed. The reserved 149-case final benchmark has
not been run.
