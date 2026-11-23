# TraceGuard LangGraph Integration V1

## Implemented boundary and inspection findings

The integration reuses the existing `POST /api/v1/runs`, normalization task, canonical trace,
Analysis coordinator, attribution and dashboard reads. It does not introduce another processing
pipeline or execute external application code inside TraceGuard.

Before implementation, inspection established:

- Operational API routes depend on `require_user`. POSTs require an existing opaque session
  cookie and an exact trusted Origin. `DEV_READ_ACCESS` permits development GET/HEAD only.
- Who&When accepts `question`/`history`; the controlled adapter accepts its own canonical
  envelope. Neither contract should be widened to accept arbitrary LangGraph state.
- Run processing is `pending → queued → processing → normalized`, or `failed`. Original bytes
  commit before publication, and claim tokens fence duplicate/stale normalization workers.
- `POST /runs/{id}/analysis` already owns one Analysis per Run, but requires normalization first.
  Its existing Celery coordinator manages attribution, specification, compilation and validation.
- Replay only supports explicitly approved developer-owned harnesses. An external LangGraph
  trace cannot authorize arbitrary agent-code execution, even if its agent names match a fixture.
- The separate frontend at `C:\Users\Administrator\traceguard_frontend` already consumes
  generic source names, canonical steps, attribution evidence and review outcomes.

## Architecture and exact data flow

```mermaid
sequenceDiagram
    participant G as External LangGraph application
    participant H as Portable recorder / HTTP helper
    participant A as Existing FastAPI
    participant D as PostgreSQL
    participant W as Existing Celery workers
    participant U as Existing dashboard
    G->>H: Execute wrapped real nodes and tools
    G->>H: Explicit exception/evaluator failure signal
    H->>A: Authenticated POST /runs?source=langgraph
    A->>D: Commit original bytes (stable execution identity)
    A->>W: Publish existing normalization task
    A-->>H: 202 with Run ID
    W->>D: Canonical steps + normalized state, atomically
    opt Caller explicitly enables automatic analysis
        H->>A: Poll Run until normalized
        H->>A: GET existing Analysis; POST only if absent
        A->>W: Existing Analysis coordinator
        W->>D: Attribution and grounded evidence
        W->>D: RegressionSpec needs_review: no external replay harness
    end
    U->>A: Existing dashboard, Run, trace, analysis and artifact GETs
    A-->>U: Actual persisted records and review reason
```

Automatic kickoff is **client-side and opt-in** (`auto_analyze=True`). There is no new server
recovery scheduler or default paid processing on every submission. The helper's wait is bounded;
after a timeout/crash, resume the same saved payload/Run. A successful submission is durable even
if the client exits before requesting analysis. Concurrent analysis POSTs still resolve to the
existing unique coordinator operation; transient duplicate deliveries cannot create new stage
objects or bypass existing single-attempt analysis semantics.

## Wire contract

Registered source: **`langgraph`**. Payload version: **`langgraph-v1`**.
The exact schema is in `app/schemas/langgraph.py`; translation is in
`app/integrations/traces/langgraph.py`. A submission contains:

| Field | Contract |
| --- | --- |
| `version` | Literal `langgraph-v1` |
| `execution_id` | UUID generated once for this external execution; retain for HTTP retries |
| `task` | Original application task |
| `failure` | Explicit kind and message; kinds: `exception`, `evaluator_failed`, `timeout`, `developer_report` |
| `events` | Nonempty ordered captured invocation/tool events |
| Event `agent_name` | Actual graph node's agent identity; tool events retain their owning agent |
| Event payload | JSON `input`, `output`, optional `tool_name`, `tool_arguments`, `tool_result` |
| Event completion | `success` or `error`, error details when failed, optional aware start/finish timestamps |

Unknown envelope fields are rejected. Event list positions become consecutive zero-based canonical
sequences, without filtering/reordering or inserting task/failure annotations as artificial agents.
Start timestamps map to `occurred_at`; finish/error/execution identities remain in metadata.
Exception details also become the canonical error observation when there is no returned output.
The complete submitted JSON bytes remain unchanged in `Run.original_trace`.

The portable recorder reserves a list position at invocation start, so nested tools and overlapping
calls preserve observed start order. This is an observation order, not a reconstructed global
causal order for concurrent graph branches. V1 supports explicitly wrapped synchronous JSON-state
nodes/tools. It does not transparently instrument arbitrary graphs, async functions, LangSmith
exports, checkpoints or every LangChain message/object type. Applications with non-JSON state
must provide an intentional JSON projection rather than serializing arbitrary objects with `str`.

## Authentication, secrets and duplicates

`examples/langgraph_refund/integration.py` is a small stdlib-only helper that can be copied into
another Python application. It does not import TraceGuard backend code, provision credentials,
or add an unauthenticated ingestion route. Supply the existing session cookie as a complete
`name=value` header and the exact configured frontend/backend Origin. HTTP is permitted only
on loopback; remote URLs require HTTPS. Redirects are refused so cookies are not forwarded.
Do not paste session cookies or API keys into chat, source files, command arguments or logs.

The current frontend's local-login placeholder is not a backend session. Real operational writes
still require a backend GitHub session, or a deliberately isolated test login in automated tests.
No machine-token/authentication framework was added. Session expiry remains an integration limit.

Known structured credential keys are removed from captured JSON snapshots, with a visible
`__traceguard_redacted_keys__` marker. Configuration/cookies/provider keys are never captured as
RunnableConfig. Exception capture defaults to class name, avoiding arbitrary secret-bearing
exception messages. This is not general free-text secret detection: use synthetic data or an
application-specific sanitization policy before recording sensitive content. Existing backend
benchmark/credential checks still fail closed before provider inference if bypassed; they were
not weakened. Original preservation refers to the **submitted, sanitized** bytes.

LangGraph submissions derive a deterministic Run UUID from a fixed namespace and external
execution UUID. PostgreSQL primary-key conflict handling and the existing publication row lock
ensure concurrent identical submissions reuse one Run and publish one normalization task. No
migration was needed. Reusing an execution UUID with different original bytes returns HTTP 409
`SUBMISSION_CONFLICT`. Even whitespace changes count as different evidence; the helper uses
stable JSON serialization. A new graph execution gets a new execution UUID.

Repeated queued/processing submissions return the same ID with 202; normalized/failed existing
Runs return 200 with their actual state. Publication errors retain the original and return 503;
repeating a failed operation does not silently reset/requeue it. Helper errors do not echo server
bodies or credentials. Transport errors are not automatically retried: retry the saved payload
explicitly, using its same execution UUID.

## Real refund application

`examples/langgraph_refund/application.py` compiles and invokes a real `StateGraph`:

1. **Intake Agent** derives 21 elapsed days from 1 September to 22 September.
2. **Policy Retrieval Agent** invokes the recorded `lookup_refund_policy` tool, returning 14 days.
3. **Eligibility Decision Agent** applies an intentional OR/AND defect and returns `approved=True`.
4. **Customer Response Agent** receives that actual result and communicates approval.

There are five canonical events because the policy node and its tool are separate invocations.
The independent evaluator checks actual dates/policy and explicitly reports the violation. A
corrected deterministic run declines the refund, supplies no failure signal and is not submitted.
No manually fabricated replacement trace is used. Deterministic agents are ordinary functions
executed by LangGraph, not LLM calls. This follows the official
[LangGraph StateGraph node/state/edge contract](https://docs.langchain.com/oss/python/langgraph/graph-api).

Setup and deterministic execution from the backend root:

```powershell
uv sync --locked --group evaluation --group langgraph
uv run --group langgraph python -m examples.langgraph_refund.run --output .local/refund-deterministic.json
uv run --group langgraph python -m examples.langgraph_refund.run --corrected --output .local/refund-corrected.json
```

LangGraph is an **optional development/demo group**, locked at 1.2.16 in this implementation.
Production ingestion does not import or require LangGraph. CI installs the group to execute the
real demonstration tests. Existing application packages were retained; the lock adds LangGraph
and its transitive dependencies rather than migrating dependency managers.

For an external application, wrap its actual functions using `recorder.node(node_name, function)`
and its tool calls using `recorder.tool(owner, name, arguments, function)`. After its independent
evaluator/exception handling, call `recorder.failed_trace(FailureSignal(...))`, then
`client.submit_failed(trace, auto_analyze=False)`. Passing no failure yields no HTTP request.
The helper never assumes every semantically incorrect answer can be detected automatically.

## Replay behavior

After attribution, the existing regression preflight recognizes canonical source `langgraph`
and persists `needs_review` with the reason that no approved external replay harness exists.
The coordinator stops as `review_required` / `regression_needs_review`. It does not call the
synthesis provider, compile a test, create a validation or execute Docker. Attribution abstention
can stop even earlier. Existing approved calculator/refund harnesses and benchmark scoring are
unchanged. Adding a separately reviewed replay target is future work, not implicit authorization
to execute submitted source code.

## Current dashboard verification

The actual persisted demonstration is available at:

[Open the LangGraph refund Run](http://localhost:5173/runs/11480b63-5265-562c-8989-7cc8105e1160)

Execution UUID: `6646948d-589a-4282-a223-d9c8497aa39c`. The execution trace is real. Attribution
for this no-cost demonstration is an **explicit deterministic fixture**, labelled in both its
explanation and model (`deterministic-integration-fixture`); it is not a measured model result.

Verification used the new code in a temporary host API on `127.0.0.1:8011`, the shared development
PostgreSQL database, a UUID-named private RabbitMQ queue and real Celery workers with injected
fake provider boundaries. Actual cookie/Origin validation used a short-lived synthetic test login;
the login was removed afterward. Existing AI workers were not restarted or sent analysis tasks.
The current API at port 8000 and the existing frontend at port 5173 read the persisted records.
No UI API responses were mocked. This avoids spending while exercising the real processing flow.

Browser checks at 1440px and 390px verified source display, five events, exact agent identities,
the decisive-step link, actual 21/14 inputs, approval output, downstream response, raw original
payload, review reason, and absent generated/validated tests. Six screenshots and network evidence
are under `.local/langgraph-v1/ui/`. The frontend's only source change is recognizing canonical
`error` as well as `failed` in existing trace-row error styling; no redesign was made.

The running port-8000 image still needs the normal rebuild/restart before accepting **new**
LangGraph submissions there. Reading the saved Run requires no new deployment. Use the existing
local frontend override if retaining development read access:

```powershell
docker compose -f docker-compose.yml -f ../traceguard_frontend/docs/local-backend.compose.yml build backend worker
docker compose -f docker-compose.yml -f ../traceguard_frontend/docs/local-backend.compose.yml up -d --no-deps backend worker
```

These deployment commands are provided for the operator; this task did not restart existing AI
workers. No validation-image rebuild is needed for external LangGraph ingestion/review.

## First live LLM demonstration: prepared, not executed

The existing `OPENAI_API_KEY` was selected for reuse. No paid requests have been made. The
separate live boundary in `examples/langgraph_refund/live_agents.py` uses official SDK structured
Responses, three LLM-driven nodes, a deterministic policy tool, SDK retries disabled, per-request
timeout/output limits and a maximum **three agent requests**. It uses the
[official structured-output API](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).
The deliberately faulty agent instruction is part of this synthetic external application; actual
returned model decisions are never replaced with a canned approval. If the model returns a
policy-compliant decision, the evaluator supplies no failure and nothing is submitted.

Before running live:

1. Review the synthetic fault prompt, model and synthetic customer data. Obtain explicit paid-call
   approval with an agreed budget. The current default is the repository's `gpt-5.6-luna` model;
   account/model access and successful provider behavior have not been live-tested here.
2. Rebuild/restart the ordinary backend/worker from current source as above. Check readiness and
   existing schema head. No database migration is introduced by V1.
3. Configure an authenticated `TRACEGUARD_SESSION_COOKIE` through an ignored environment file
   or process environment, plus `TRACEGUARD_URL` and the trusted `TRACEGUARD_ORIGIN`. Keep the
   reused OpenAI key out of the trace. Backend OAuth registration/login is required for a real
   session; the local frontend placeholder does not issue one.
4. After approval, run the command below with a **new output path**. Output is saved before HTTP
   submission. Keep its payload/UUID for any explicit submission retry.

```powershell
# PAID OPT-IN — do not execute without approval.
uv run --group langgraph python -m examples.langgraph_refund.run --live --submit --output .local/refund-live-v1.json
```

That command permits up to three agent requests and only ingests a reported failure. Adding
`--auto-analyze` separately permits the normal TraceGuard attribution request after normalization.
For a clean new LangGraph Run under the existing single-attempt coordinator policy, this adds at
most one attribution attempt; unsupported replay preflight makes no synthesis/Docker call. Budget
for **up to four model requests in total**, and account for uncertain external failures rather than
claiming exactly-once billing. The three-call demo budget is per process, not a durable billing ledger.

Inspect the real result in the dashboard. An incorrect attribution or abstention must be retained;
do not replace it with the deterministic fixture or silently rerun it to obtain a preferred answer.
The reserved final benchmark and its freeze files are unrelated and must not be run or rewritten.

## Verification and remaining limits

See [langgraph-integration-v1-results.md](langgraph-integration-v1-results.md) for exact test outcomes,
artefact paths, encountered issues and checks of protected evaluation files. Core test commands:

```powershell
uv run --group evaluation --group langgraph pytest tests/unit/test_langgraph.py tests/integration/test_langgraph.py -q
uv run --group evaluation --group langgraph pytest -m 'not live_llm and not sandbox' -q
python scripts/check_langgraph_ui.py
```

Integration tests require a configured dedicated `_test` PostgreSQL database and test broker URL;
each owns an isolated schema/queue. The browser command uses the installed Python Playwright
environment, the existing UI, and the recorded demonstration Run. It performs GETs only.

V1 deliberately has no machine authentication, universal callback exporter, async/subgraph support,
arbitrary state serializer, semantic failure detector, external replay engine, durable client-side
submission scheduler or live accuracy claim. The legacy final evaluation source pin will differ
after integration changes; no scoring, manifests, freezes or historical results were modified.
