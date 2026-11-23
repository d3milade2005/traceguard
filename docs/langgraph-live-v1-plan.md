# Minimal live LangGraph verification: approval plan

Prepared on **21 October 2026 (Africa/Lagos)**. **Paid execution is not approved or performed.**
This is a single development end-to-end experiment, separate from the reserved 149-case final
benchmark. The earlier deterministic attribution fixture is not a live result.

## Exact inference plan and cost

| Work | API model | Reasoning effort | Planned requests | Maximum output tokens per request |
| --- | --- | --- | --- | --- |
| External Intake Agent | `gpt-5.6-luna` | `none` | 1 | 2,048 |
| External Eligibility Decision Agent | `gpt-5.6-luna` | `none` | 1 | 2,048 |
| External Customer Response Agent | `gpt-5.6-luna` | `none` | 1 | 2,048 |
| TraceGuard failure attribution | `gpt-5.6-terra` | `high` | 1 if a failure is submitted | 8,192, including reasoning |
| Policy Retrieval Agent/tool | Local explicit 14-day policy lookup | Not applicable | 0 model requests | Not applicable |
| Regression preflight, normalization, UI | Existing deterministic application mechanisms | Not applicable | 0 model requests | Not applicable |

**Maximum four inference attempts for one graph execution.** SDK retries are disabled. The
existing Analysis coordinator allows one attribution attempt per Analysis. Do not start a
standalone attribution or silently rerun after failure/timeout. A compliant graph response
produces no evaluator failure, so only the three agent requests occur and no Run is submitted.

Official standard prices, verified on the preparation date:

- [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna):
  **$0.20 / million input tokens; $1.20 / million output tokens**.
- [GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra):
  **$2.00 / million input tokens; $12.00 / million output tokens**; supports `high` effort.
- [Reasoning token accounting](https://developers.openai.com/api/docs/guides/reasoning):
  reasoning is billed as output and consumes the output-token limit.

Planning estimate: **approximately US$0.02–$0.10**, with an approval allowance of **US$0.25**.
This estimate assumes small synthetic text inputs, standard service, no hosted tools or cache
discounts, roughly 2,000–5,000 Terra input tokens and 1,000–6,000 Terra output/reasoning tokens.
The three Luna calls should contribute less than a cent at their configured output limits and
small inputs. At 5,000 Terra input tokens and the full 8,192-token output limit, Terra costs
about **$0.1083**; actual cost depends on returned usage. The allowance is not a provider-enforced
billing cap. Preserve uncertain provider failures; do not claim that a missing usage response
means a request was free. No separate paid connectivity test, corrected run or synthesis call
is included. Account/model access has not been inferred from possession of an API key.

Use the existing approved key. The external process selects Luna explicitly; the backend and
worker select Terra High explicitly. This prevents a shared `.env` model value from accidentally
running all agent nodes on Terra or attribution on Luna.

## Regular runtime and preflight

The ordinary `traceguard-backend` and `traceguard-worker` images have been rebuilt from current
source. They use the existing PostgreSQL/RabbitMQ services and normal queue `traceguard`.
Neither a temporary attribution API nor fake model providers will be used for live verification.

Local experiment configuration is in ignored files:

- `.local/langgraph-live-v1/compose.yml`: Terra High, 8,192 output tokens, 180-second timeout.
- `.local/langgraph-live-v1/compose.no-inference.yml`: blank API key overrides for both API and
  worker while paid approval is pending. The existing stored key is unchanged.

The existing frontend local-development Compose override retains its explicit CORS/read setup.
The normal API remains at `http://localhost:8000`; frontend remains at `http://localhost:5173`.
Only the regular API and ordinary worker are recreated. PostgreSQL/RabbitMQ data and the
validation worker/image are retained. LangGraph remains an external optional demo dependency.

The frontend retains its existing local development access. The essential transport change
adds `credentials: 'include'` so an available backend session is sent on API requests. There is
no new auth scheme, route or dashboard redesign. Development read access remains explicit in
the existing local Compose override; operational POSTs still require authentication.

GitHub OAuth credentials were initially absent; the user configured them locally and the normal
start route was verified to redirect to GitHub with an HttpOnly binding cookie. The user then
explicitly removed GitHub login from the experiment's requirements. Frontend OAuth controls
prepared during inspection were reverted to retain existing local access. The final workflow
therefore uses a short-lived **local verification session**, exercising the existing cookie and
Origin checks. It makes no claim to validate a genuine GitHub identity exchange. That test
identity is independent of attribution: all approved inference must use actual providers.

The local verification session is stored only in ignored
`.local/langgraph-live-v1/session.env` and never printed or copied into traces. The helper sends
trusted Origin `http://localhost:5173` and targets `http://localhost:8000`. Remove the temporary
session/account after verification. A database user row alone is not sufficient authentication.

## Execution after approval and authenticated setup

1. Record approval, the maximum four attempts and the agreed US$0.25 allowance. Check the
   normal queue for unrelated pending inference and confirm the local verification session is valid.
2. Remove only the no-inference override from the Compose invocation and recreate API/worker
   to restore their existing key. Keep Terra/high/output/time limits; do not overwrite the key.
3. Execute `examples.langgraph_refund.run --live --submit --auto-analyze` **once**, with external
   `OPENAI_MODEL=gpt-5.6-luna`, the verification session, trusted Origin and a new evidence output path.
   No live command was run during preparation.
4. The actual LangGraph nodes and policy tool produce recorded events. A separate local evaluator
   reports a failure only if the actual decision approves the request after 21 days despite the
   14-day policy. The intentional faulty decision instruction is disclosed experimental fault
   injection. Never replace a real model output with a canned approval.
5. The helper saves real evidence before HTTP submission and automatically requests existing
   Analysis only after normalization. Poll that one Run/Analysis; reuse its identity for any
   ingestion retry without rerunning the graph or resampling attribution.
6. Compare the actual attribution with the predeclared expected **Eligibility Decision Agent,
   canonical sequence 3 (fourth event)**. This expectation belongs in the experiment report,
   not an uploaded ground-truth label. Retain an incorrect answer or abstention unchanged.
7. Inspect the regression record and final Analysis status. V1 has no approved external replay
   harness. Its preflight should persist `needs_review`, with Analysis `review_required` and
   stop reason `regression_needs_review`. It should make **zero synthesis requests**, create
   no generated test or validation, and run no uploaded code in Docker. Abstention can stop
   earlier. A pipeline error must be reported as an error, not relabelled as successful review.
8. Verify the actual Run in the existing local-development frontend at desktop and mobile widths.
   Capture the source/Run ID, actual agent/tool steps and payloads, attribution/evidence,
   requested and returned models, review reason and absent test/validation artifacts.
9. Write a concise report from the actual saved trace, model usage and API records. Record
   requested/returned model IDs, response IDs, input/output/reasoning tokens, measured latency,
   calculated standard-rate cost, observed attribution correctness, screenshots and limitations.

The example now saves per-agent provider usage separately from captured graph state, including
response IDs, returned model, token usage, effort and latency. TraceGuard already persists its
attribution usage. These records support actual reporting without leaking billing metadata into
the attribution evidence. A partial/failed response may lack full usage and must be reported so.

## Research interpretation

One successful example demonstrates interoperability and a single observed attribution result.
It does not establish population accuracy, regression fault-catch rates, performance/scalability
or reliability under repeated runs. A `review_required` external replay result is honest behavior,
not a validated regression. No benchmark scoring code, freeze, final manifest or reserved final
evaluation is to be executed or rewritten.

Authentication scope is deliberately limited to the normal cookie/Origin validation exercised
with a local test session; OAuth account ownership is not verified. The local frontend access
placeholder and development-only public read setting remain implementation limitations.

The final thesis-ready experiment report remains pending live approval and execution. Its Run
ID, model correctness, real usage and screenshots must come from the new live Run; do not reuse
the prior fixture Run as evidence that paid inference succeeded.
