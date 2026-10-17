# Final backend review, 2026-10-17

The backend contracts are ready for frontend implementation. GitHub OAuth must be
registered/configured and migration `0008` applied in the deployment environment.
No frontend code, Redis, new service, or paid provider request was added in this pass.
The 149 final records have no predictions; their final experiment requires separate
explicit authorization.

## Offline development review

The original 16-case result remains agent 9/16, step 6/16, joint 6/16, one abstention,
one structured-output failure, and coverage 14/16. Historical outputs are unchanged.
All ten non-joint-correct cases were reviewed against original history, canonical
steps, selected context, prediction/explanation/evidence, and raw labels.
The [machine-readable review](../research/reviews/attribution-development-review-v1.json)
preserves full IDs, context visibility, digests, and case-level reasoning.

| Sample prefix | Primary classification | Observation |
| --- | --- | --- |
| 0d8b79c04fad | abstention | Full OCR evidence supports the annotated later substitution; explanation misreads the top-five/top-ten distinction. |
| 3c12f235ac81 | wrong_agent | Chooses a later Statistics mock instead of earlier HistoricalWeather placeholder API. |
| 3c310f4a7a83 | structured_output_failure | Rejected historical body was not retained; its precise schema violation remains unknown. |
| 0c3988a06834 | wrong_agent | Chooses WebSurfer's later unsupported claim instead of Orchestrator's earlier action. Both are visible; adapter speaker/recipient mapping is correct. |
| bd19293a21a5 | context_evidence_missing | Annotated step 32 was omitted from a 130-step history; predicts final unsupported answer at 129. This does not prove pruning caused the error. |
| 568fafbf10ab | benchmark_semantic_disagreement | Earlier failed extraction/empty data versus later benchmark-annotated verification/recovery. |
| 5e0abb29aec1 | correct_agent_wrong_step | Same agent, but chooses synthetic-data event 3 rather than assumed-file event 1; explanation also misattributes another step. |
| 5a1a837b70b7 | correct_agent_wrong_step | Chooses the terminal error at 5, versus the annotated later extraction/search at 8. |
| 832f164cdecd | benchmark_semantic_disagreement | Unsupported menu claim at 6 versus benchmark's earlier retrieval plan at 1. |
| 917ea0918e79 | benchmark_semantic_disagreement | Earlier Physics action/NameError versus later ModelEvaluation placeholder. |

Primary counts: benchmark disagreement 3, wrong agent 2, correct agent/wrong step 2,
missing context 1, abstention 1, structured failure 1. Secondary classifications can
overlap. A plausible alternative causal explanation is not a corrected benchmark label.
All 16 remain eligible; nullable optional annotations do not exclude these cases.

One of the ten errors lacks its annotated step; the other nine retain it. The three
Hand-Crafted errors do not demonstrate systematic speaker/recipient confusion.
Evidence-result-step choices recur, but `attribution-v1` already asks for originating
causes and ambiguity-aware abstention, and benchmark disagreements run in both
earlier/later directions. No consistent minimal prompt correction is established.
Keep `attribution-v1`; no tuning of context, model, reasoning, scoring, or manifest.
High confidence is poorly calibrated in this small development sample; no broad
calibration claim is justified.

## Replay contract correction

RegressionSpec provenance validation answers whether a cited value exists in recorded
evidence. A nested scalar can satisfy that contract while failing the target harness's
invocation contract. The compiler faithfully serialized that scalar; both runtime
variants then crashed. This was not a Docker failure.

The compilation application service now checks the existing approved harness after
provenance/snapshot verification and before deterministic compilation. Unsupported
input produces a durable failed artifact with `REPLAY_CONTRACT_INCOMPATIBLE`, no Python
source, and no sandbox execution. The analysis coordinator stops as `review_required`
and retains that artifact ID. Repeated requests reuse the failed attempt.

The evaluation harness shares a pure invocation validator with its actual runtime.
Track B performs that check at the explicit `replay_contract` stage before compiling.
`controlled-suite-replay-v1` identifies this check. No scalar is wrapped/repaired, no
oracle is exposed to prompts, the harness is not weakened, and `regression-spec-v1`
remains unchanged. A synthesizable model disposition can still be unreplayable; the
new metric reports replay compatibility separately.

The original live results remain 6/6 attributed, synthesizable, and compiled; 3/6
valid and 3/6 inconclusive. The six deterministic fixture checks now pass through real
Docker with faulty assertion failure and corrected pass. These are offline verification
results, not a new 6/6 model experiment or an improvement to historical accuracy.

An [offline check of the six saved model-selected fixtures](../research/reviews/regression-replay-review-v1.json)
confirms exactly three compatible and three incompatible values. It makes zero Docker
executions/provider calls and retains the original snapshots and historical verdicts.

## Safe invalid-output diagnostics

`structured-output-diagnostics-v1` retains provider response ID/usage/latency plus
schema name/hash, prompt version, response digest, error count/category, and bounded
schema-owned field paths. Unknown field names are masked; rejected values, validator
messages/context, raw rejected output, and hidden reasoning are excluded. Diagnostics
are persisted on attribution/spec operations and in future research failures. Existing
historical failed samples remain unchanged and cannot acquire reconstructed diagnostics.

## Frontend contracts and security

The [frontend contract](frontend-contract.md) supplies methods, parameters, examples,
status codes, cookie behavior, and setup commands. Existing detail/stage APIs remain;
the existing run detail gains lightweight stage IDs. New routes are the paginated
`GET /api/v1/runs`, `GET /api/v1/dashboard`, `GET /api/v1/system`, and four `/auth/*`
routes. Dashboard aggregates durable PostgreSQL rows; no duplicate state is persisted.
Ledger SQL excludes original bytes, context bodies, and generated source.

GitHub authorization code with S256 PKCE uses browser-bound, expiring, single-use
state. Backend-only token exchange uses fixed HTTPS destinations and no redirects;
the GitHub token is discarded after profile lookup. Opaque app sessions are hashed
in PostgreSQL, expire after 12 hours by default, rotate on login, and revoke on logout.
Cookies are HttpOnly, host-only, SameSite=Lax and Secure with `__Host-` names outside
local development. Unsafe operations require an exact trusted Origin. CORS uses
explicit credentialed origins. OAuth/profile tests use fakes; no real GitHub login
was claimed. All authenticated users share all runs; there is no per-run ownership.

Migration `0008` adds users, auth sessions, OAuth flows, nullable safe diagnostic JSONB
columns, and ledger/session expiry indexes. Upgrade/downgrade and ORM consistency
are tested in disposable schemas. The existing local application database was then
upgraded from `0007` to `0008` using the reviewed source; `alembic current` reports
head and `alembic check` reports no pending changes. Existing experiment/trace values
were not rewritten. Other deployment databases must run `uv run alembic upgrade head`.

Phase 7 retains explicit built-in seccomp and all existing isolation/resource controls.
Its security probes check non-root, read-only root, capabilities, no-new-privileges,
seccomp, network, credentials, mounts/socket absence, bounded tmpfs/memory/PIDs,
timeouts, and execution-error classification. Generated code executes only there.
Test controllers can access Docker to launch these containers, as the validation
worker does; generated-test containers never receive that socket or repository.

## Final evaluation preparation

`phase8-runner-v2`/`phase8-freeze-v2` add explicit code/data pins and offline baseline
equivalence verification. Both original prompts and provider configuration remain.
All 16 original request/context digests and deterministic scores were reconstructed
and matched without invoking a provider. This supports freezing hardening changes
without inventing a new live development result. A changed inference prompt/configuration
would require a separately authorized development run instead.

The current freeze artifact at `research/frozen/who_when-final-v2.json` pins source/data,
the complete final manifest, model/effort, prompts, context, scoring/semantics, timeout,
output limit, runner, review, and verified baseline. Its source digest includes
uncommitted relevant source; the recorded Git revision alone is insufficient.
Freeze creation is exclusive; changes require a new artifact/version. The runner
verifies the complete 149 IDs and every pinned setting, saves each attempt, resumes
without retrying wrong/failed answers, and refuses overwrite or uncertain-call replay.

Freeze v1 remains preserved. V2 supersedes it solely because three pre-existing
formatter differences were corrected to make the repository-wide CI check pass:
an end-of-file newline, an empty whitespace line, and a documentation code example.
All 16 saved requests/contexts/scores matched again before issuing v2. Neither prompt,
model behavior, context selection, nor scoring changed. No final attempt used v1.

The exact future command, **not executed**, is:

```powershell
uv run --group evaluation python -m scripts.evaluate_attribution --manifest research/manifests/who_when-v1.json --partition final --freeze research/frozen/who_when-final-v2.json --output .local/evaluation/final-luna-medium-v1 --max-samples 149 --max-calls 149 --model gpt-5.6-luna --reasoning-effort medium --live
```

After authorization, resume that exact command with `--resume` if interrupted. Inspect
the printed preflight and immutable artifact first. The research runner uses its local
provider configuration and does not bypass or depend on browser authentication.

## Remaining limits

The 16-case development sample is coverage-oriented, not a representative estimate.
The full audit already shows 19 eligible annotated steps omitted by Phase 3; this
policy remains unchanged. Three annotations admit plausible alternative causal
interpretations, without rescoring. Original model replay incompatibilities remain
experimental failures; deterministic rejection makes them explainable, not successful.

Local Windows host access to Docker's PostgreSQL forwarded port timed out, so real
DB/broker verification used an ephemeral trusted controller on the existing Compose
network, disposable test schemas, and dedicated test queues. No production connectivity
logic or sandbox restriction was changed. Configure real GitHub OAuth credentials,
apply migration head, and verify the deployment's browser origins/forwarding before use.
Frontend pages, thesis charts, and final benchmark interpretation remain separate work.

## Verification

The final unit run passed 675 tests. The full offline PostgreSQL/RabbitMQ integration
run initially passed 111 and failed three test fixtures. The corrected three and new
analysis-review path passed in a six-test recheck, covering 115 distinct integration
tests across the runs. The corrections use a deep JSON copy and select only historical
columns during downgrade checks; no migration behavior was weakened. Both diagnostic
persistence paths also passed. The 17 opt-in Docker checks passed, including six suite
cases and the persisted fake-inference E2E. Two paid suites were excluded.

Ruff passed repository linting; all 74 changed/new Python files and all 200 repository
formatter inputs passed formatting;
mypy passed all 106 application files. `git diff --check`, Compose validation-profile
configuration, real migration upgrade/downgrade/schema checks, saved-development
equivalence (16/16), and a frozen final dry plan (149 remaining, zero calls) passed.
The final experiment output path was not initialized by that separate dry plan.
Existing Starlette/AnyIO deprecation warnings remain.

The [machine-readable verification record](backend-final-verification-v2.json)
includes checks, source/freeze identities, protected historical artifact digests,
and the complete changed-file inventory (including earlier uncommitted Phase 8 work).
