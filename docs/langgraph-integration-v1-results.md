# LangGraph Integration V1: verification record

Verification date: **21 October 2026**. These are development integration results, not final
research evaluation results. No paid model requests, reserved final evaluation, benchmark
rescoring, arbitrary uploaded-code execution or new Docker validation runs were performed.

Architecture, setup, authentication and live-demo instructions are in
[langgraph-integration-v1.md](langgraph-integration-v1.md).

## Implemented files

| Files | Change and purpose |
| --- | --- |
| `app/schemas/langgraph.py` | Strict versioned external trace, event and explicit failure contracts |
| `app/integrations/traces/langgraph.py` | Framework boundary translating actual captured events into canonical steps |
| `app/integrations/traces/sources.py` | Register `langgraph` alongside existing sources |
| `app/services/runs.py` | Stable-identity insertion using the existing PostgreSQL primary key; conflicting evidence rejected |
| `app/services/submission.py` | Preserve original bytes, record external failure kind, deduplicate LangGraph executions and reuse existing locked publication handoff |
| `app/schemas/runs.py`, `app/api/v1/runs.py` | Return actual reused Run state, with 200 for terminal ingestion states and 202 for in-flight states |
| `app/core/exceptions.py`, `app/api/errors.py` | Safe 409 `SUBMISSION_CONFLICT` response |
| `app/services/regression_input.py` | External LangGraph replay preflight returns an explicit developer-review reason |
| `examples/langgraph_refund/integration.py` | Portable synchronous recorder, structured credential filtering and authenticated HTTP client; optional client-side analysis kickoff |
| `examples/langgraph_refund/application.py` | Executed four-agent LangGraph refund scenario, real policy tool, faulty and corrected deterministic modes |
| `examples/langgraph_refund/live_agents.py` | Separately opted-in, budgeted provider implementation; not run against a provider |
| `examples/langgraph_refund/run.py` | CLI preserving captured evidence before submission |
| `tests/unit/test_langgraph.py` | 17 focused cases, including parametrized cases |
| `tests/integration/test_langgraph.py` | Six PostgreSQL/authentication/HTTP/coordinator/RabbitMQ integration cases |
| `tests/integration/test_validations.py` | Isolate an existing real-broker test from the operational validation queue |
| `scripts/check_langgraph_ui.py` | Read-only browser checks against the real persisted Run and existing UI |
| `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml` | Optional locked demo dependency group; CI installs it to run actual LangGraph tests |
| `README.md`, these two documents | Integration documentation and measured verification record |
| Separate frontend: `src/components/TraceStepRow.tsx` | One conditional accepts canonical `error` alongside existing `failed` for error styling |

Existing uncommitted refund-harness, execution-sandbox and thesis-evidence work was retained.
No new database migration, endpoint family, authentication mode or processing pipeline was added.

## Test results

All test model boundaries use fakes. PostgreSQL tests use `traceguard_test`, disposable schemas
and private queues. Test credentials are supplied locally without copying their values into
commands or this document.

| Verification | Actual result | Evidence |
| --- | --- | --- |
| Focused LangGraph integration tests | **6 passed**, 30.39 seconds | `.local/langgraph-v1/integration-tests.xml` and `.log` |
| First broad offline suite | **816 passed, 1 failed, 19 deselected**, 337.72 seconds | `.local/langgraph-v1/full-offline-tests.xml` and `.log` |
| Queue-isolation fix recheck | **1 passed**, 5.70 seconds | `.local/langgraph-v1/validation-queue-recheck.xml` and `.log` |
| Final broad offline suite | **817 passed, 19 deselected**, 287.81 seconds | `.local/langgraph-v1/full-offline-final.xml` and `.log` |
| Ruff lint, repository-wide | Passed | `python -m ruff check .` |
| Ruff formatting, repository-wide | Passed, 222 files already formatted | `python -m ruff format --check .` |
| Existing strict application mypy scope | Passed, 109 source files | `python -m mypy app` |
| Dependency lock consistency | Passed, 82 packages resolved offline | `uv lock --check --offline --cache-dir .local/uv-cache` |
| Existing frontend typecheck | Passed | `npm.cmd run typecheck` in the frontend repository |
| Existing frontend production build | Passed, 1,644 modules, 33.57 seconds | `npm.cmd run build` in the frontend repository |
| Real browser verification | Passed at **1440px and 390px** | `.local/langgraph-v1/ui/results.json`, six screenshots, `ui-check.log` |
| Protected research/evaluation inventory | **26/26 files byte-identical** to the pre-integration thesis inventory | `.local/langgraph-v1/verification.json` |

The first broad suite executed 817 cases: **696 unit and 121 integration**. Its only failure
was `test_dedicated_rabbitmq_worker_flow_uses_fake_runner`, which published to the normal
`traceguard.validation` queue. A running development validation worker could consume the test
message instead of its fake worker. The fix gives that test a UUID-named auto-delete queue and
routes its real publisher to that queue. Production queue routing is unchanged. The focused
recheck passed, followed by a clean full offline run of all 817 cases. The initial failure is
retained rather than being erased from the record. All 23 new LangGraph cases passed in that
final run: 17 unit and six integration.

The broad suite excludes the `live_llm` and `sandbox` markers. This is not evidence of fresh
Docker sandbox execution or live model correctness. The Starlette test client emits an existing
AnyIO `BlockingPortal` deprecation warning. It did not cause test failures.

Commands actually used, from the backend root with dedicated test URLs configured:

```powershell
.venv/Scripts/python.exe -m pytest tests/integration/test_langgraph.py -q -p no:cacheprovider --junitxml=.local/langgraph-v1/integration-tests.xml
.venv/Scripts/python.exe -m pytest -m 'not live_llm and not sandbox' -q -p no:cacheprovider --junitxml=.local/langgraph-v1/full-offline-final.xml
.venv/Scripts/python.exe -m pytest tests/integration/test_validations.py::test_dedicated_rabbitmq_worker_flow_uses_fake_runner -q -p no:cacheprovider --junitxml=.local/langgraph-v1/validation-queue-recheck.xml
python scripts/check_langgraph_ui.py
```

The browser command used the existing global Python Playwright installation. Playwright was
not added as a backend runtime dependency. Local `.local/` artifacts are intentionally ignored
by Git; preserve them separately if using screenshots or XML as thesis evidence.

## Focused behavior coverage

Unit tests verify identity/value/timestamp retention, invalid version/UUID/failure/empty events,
rejection of unexpected envelope labels, actual nested tool execution, safe exception capture,
no submission without an explicit failure, structured credential removal, existing backend
credential-filter rejection, analysis lookup before kickoff, no retry after authentication or
service errors, timestamp validation, actual faulty/corrected graph executions and a mocked
live-client call budget. The 17 total includes parametrized invalid-payload and HTTP-error cases.

The six integration cases verify:

1. Actual graph execution enters the existing API, preserves original bytes and failure kind,
   normalizes to five consecutive canonical events and appears in dashboard read APIs.
2. Four concurrent identical submissions produce one Run and one normalization publication;
   repeated normalization leaves exactly five stored steps.
3. Publication failure keeps original evidence, returns 503, and does not silently requeue;
   changed evidence with the same execution identity returns 409.
4. Missing session returns 401; untrusted Origin returns 403; absent failure returns 422.
   No rejected submission creates a Run.
5. Existing Analysis coordinator uses one fake attribution, records regression `needs_review`,
   stops at `review_required`, makes zero synthesis calls, and creates no test/validation.
6. Portable helper uses real loopback HTTP, existing authentication and a real private
   RabbitMQ/Celery flow. A repeated submission/kickoff reuses the same Run/Analysis and does not
   make another attribution call. An invalid session is rejected through real HTTP.

## Persisted demonstration and UI evidence

Run: **`11480b63-5265-562c-8989-7cc8105e1160`**.

[Open the existing dashboard Run](http://localhost:5173/runs/11480b63-5265-562c-8989-7cc8105e1160).

| Observed field | Actual persisted value |
| --- | --- |
| External execution UUID | `6646948d-589a-4282-a223-d9c8497aa39c` |
| Source / original version | `langgraph` / `langgraph-v1` |
| Explicit failure | `evaluator_failed`: refund approved outside the 14-day policy |
| Normalization | `normalized`, five events, sequences 0 through 4 |
| Agent identities in order | Intake Agent; Policy Retrieval Agent; Policy Retrieval Agent (tool); Eligibility Decision Agent; Customer Response Agent |
| Actual policy tool | `lookup_refund_policy`, 14-day result |
| Actual decisive invocation | Sequence 3 receives 21 days and 14-day policy, returns `approved: true` |
| Actual downstream invocation | Sequence 4 receives `approved: true`, returns “Your refund is approved.” |
| Attribution mode | **Deterministic injected fixture; zero model inference** |
| Attribution explanation/model | Explicit “no LLM inference” label / `deterministic-integration-fixture` |
| Analysis ID | `11d66a95-f0fa-4081-8fab-328532e3f6df` |
| Final Analysis state | `review_required`, stop reason `regression_needs_review` |
| Regression review reason | External LangGraph has no approved replay harness; developer review required |
| Generated test / validation | Neither created; UI displays pending/absent artifacts |
| Provider / synthesis calls | **0 / 0**; injected attribution fixture invoked once |

The trace was captured from `StateGraph.compile().invoke(...)`. Only the attribution boundary
was replaced with a labelled fixture. This validates wiring, storage and display; it does not
measure attribution accuracy. The deterministic corrected graph declines and supplies no
failure signal in tests. It is not evidence that a generated regression test caught a fault or
accepted a correction: external replay is unsupported and no such test was generated.

Processing used a temporary current-code host API on port 8011, the existing development
PostgreSQL database and a private Celery/RabbitMQ queue. Current port-8000 API/dashboard reads
then displayed the resulting records without mocks. The normal development workers were not
restarted. A final check confirmed **zero remaining synthetic demonstration users** and that
the temporary API was no longer listening. The saved Run intentionally remains for inspection.

Browser assertions checked the actual dashboard row, source, four agent identities plus tool
event, decisive-step navigation, 21/14 inputs, approval output, tool payload, downstream response,
raw original payload, review explanation, absent generated test and pending validation. Both
viewports had no JavaScript page errors, no horizontal overflow, seven successful API GETs and
no failed API requests. No response fixtures were used.

Evidence files:

- `.local/langgraph-v1/demo-result.json`: persisted IDs, canonical/original trace, fixture/call labels.
- `.local/langgraph-v1/submitted-trace.json`: actual captured external execution.
- `.local/langgraph-v1/deterministic.json`: standalone CLI execution evidence.
- `.local/langgraph-v1/ui/results.json`: real GET response statuses and browser error records.
- `.local/langgraph-v1/ui/dashboard-{1440,390}.png`: existing dashboard.
- `.local/langgraph-v1/ui/trace-{1440,390}.png`: actual execution payload and step display.
- `.local/langgraph-v1/ui/review-{1440,390}.png`: honest absent/pending test-validation display.

## Encountered issues and practical limits

- The frontend already understood generic sources and review states. Only its error-style
  condition needed canonical `error` support. The semantic refund failure itself has successful
  node statuses, so it is represented by its explicit evaluator signal and attribution.
- The frontend local-login placeholder does not establish an operational backend session.
  Tests exercised actual cookie/Origin dependencies with isolated synthetic sessions. A live
  external client needs an actual backend login; V1 adds no API-key authentication shortcut.
- Local host database configuration pointed to port 5432, while this stack forwards its
  PostgreSQL on 5433. Verification supplied the correct connection privately. A local invalid
  `DEBUG` environment value was overridden with `False` in the verification harness. Production
  configuration files were not rewritten.
- Frontend build initially could not write its TypeScript build information under filesystem
  sandbox rules. It passed when run with permission in its own repository. Offline lock checking
  likewise required permitted cache access; the lock check passed afterward.
- An exploratory mypy invocation over both `app` and namespace-style `examples` reported a
  duplicate module path. The repository's configured strict scope is `app`; that scope passed.
  The demo was exercised through Ruff, real execution and focused tests, not claimed to pass a
  separately configured strict example-package type check.
- The first browser assertion looked for an empty-state heading that the existing component
  renders as paragraph text. The check was corrected to the actual accessible text; the UI was
  not changed to satisfy a mistaken selector.
- The existing port-8000 image still needs an ordinary backend/worker rebuild before accepting
  new LangGraph submissions. Current UI verification reads the saved Run produced by current
  source in the temporary host process. This is not a claim that the old image was upgraded.
- Structured-key redaction is not a guarantee that free-text inputs/outputs contain no secrets.
  V1 captures explicit synchronous JSON state and observed invocation start order; async graphs,
  arbitrary message objects and reconstructed distributed causality remain unsupported.
- Client polling/kickoff is bounded and opt-in. An interrupted client may need to resume the
  same saved execution. Existing database/broker handoff is not a transactional outbox or an
  exactly-once inference-billing guarantee.
- The live application is prepared and mocked at its provider boundary, but account/model
  access, live attribution quality, paid-call cost and live refund behavior remain unverified.

The research/evaluation hash check compared all inventoried `evaluation/`, `research/` and
`docs/evaluation*` files with `thesis_evidence/artifacts/source-inventory.json`: **26 unchanged**.
Existing frozen source pins naturally refer to an earlier tree; they were not regenerated or
presented as covering this new integration. No final benchmark result is claimed.

The local verification record also includes Python **3.12.10**, installed LangGraph **1.2.16**,
base commit `683e2c3574365eb8e3b4ed0287b04d4025545aa8`, and SHA-256 hashes for the nine new
integration/demo/test Python files and the frontend trace component. Implementation changes
remain in the working tree; the base commit alone does not identify their contents.
