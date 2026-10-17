# Phase 8: evaluation and experiments

Phase 8 adds repository research commands, not production inference behavior.
Track A measures attribution on Who&When. Track B measures regression synthesis
on actual replayable controlled executions. Benchmark traces never acquire invented
corrected implementations. Production prompts, phases, endpoints, migrations, and
database tables were unchanged by the initial tooling implementation. Subsequent
[Docker recovery](phase8-development-results-v1.md) explicitly selects built-in
seccomp in the existing runner; all sandbox guarantees remain enforced.

## Confirmed benchmark protocol

The inspected upstream revision is
`f4d2b6da464a826580e59b3a0eae15ea2d642d7c`. URLs and SHA-256 hashes are recorded in
[evaluation-protocol-sources.json](evaluation-protocol-sources.json).

The [paper, sections 2 and 3.2](https://arxiv.org/html/2505.00212v1#S2) defines the
responsible agent and earliest decisive mistake whose correction can change the
failed outcome. `mistake_agent` and `mistake_step` carry these annotations.

The [official inference implementation](https://github.com/ag2ai/Agents_Failure_Attribution/blob/f4d2b6da464a826580e59b3a0eae15ea2d642d7c/Automated_FA/Lib/utils.py#L62-L84)
explicitly illustrates positions 2 and 3 for the third and fourth entries. Its
step-by-step loop enumerates the complete history starting at zero, without role
filtering. Human, system, and empty entries therefore count. The canonical adapter's
zero-based sequences match this operational convention. The paper's abstract
one-based mathematical notation is not an instruction to shift dataset labels.

The [upstream scorer](https://github.com/ag2ai/Agents_Failure_Attribution/blob/f4d2b6da464a826580e59b3a0eae15ea2d642d7c/Automated_FA/evaluate.py#L40-L98)
uses case-sensitive substring checks on stringified labels and divides by all
reference JSON files. It does not range-check step annotations. Missing labels
produce no correct matches while remaining in that denominator. Consequently,
that implementation can match step `1` against prediction `10`.

Our version `strict-exact-no-agent-normalization-v1` reports exact equality, not
the upstream substring behavior. Integer steps and canonical decimal strings are
accepted; whitespace, signs, leading zeroes, floats, and booleans are not repaired.
Agent labels require an exact observed agent identity. Case variants remain raw
and are excluded from agent scoring. Speaker disagreement is flagged without
changing independent agent/step eligibility. In-range step labels remain eligible
even when the agent annotation is ineligible. Joint scoring requires both labels.
Anomalous labels are research metadata and cannot fail production normalization.
These metrics and denominators must accompany any comparison with published results.

The upstream inference code also supplies a task answer when `ground_truth` exists.
This project's input omits answer labels and attribution labels. Comparisons must
therefore account for the paper's with/without-answer conditions as well as the
different split and scoring rules.

## Local context audit and split

The zero-cost audit uses the current `ContextPolicy`: 65,536 UTF-8 bytes with 4,096
reserved, two head steps, four tail steps, and one neighbor. These are serialized
byte estimates, not model token estimates. The policy has not been tuned to labels.

| Measure | Algorithm-Generated | Hand-Crafted | Total |
| --- | ---: | ---: | ---: |
| Records processed | 126 | 58 | 184 |
| Full context | 124 | 32 | 156 |
| Pruned context | 2 | 26 | 28 |
| In-range step labels | 126 | 55 | 181 |
| Decisive step retained complete | 126 | 36 | 162 |
| Decisive step omitted | 0 | 19 | 19 |
| Decisive step truncated | 0 | 0 | 0 |

The final audit includes deterministic evidence-derived UUIDs in the serialized
context envelope, matching the size used by production. Labels cannot affect
these identifiers. The initial audit with `run_id=None` is preserved separately
as v1; v2 is the production-envelope measurement. Selection and retention counts
are identical between the two measurements.

Overall complete decisive-step retention is 162/181 (89.50%); Hand-Crafted
retention is 36/55 (65.45%). Mean original size is 35,289.85 bytes and selected
size is 23,519.49 bytes. Total selected/original bytes is 0.6665. No adapter or
context failures occurred. Omission can prevent a supported diagnosis; these
measurements do not establish that pruning causes attribution errors.

Annotations include three out-of-range steps, two case variants, eight agent/step
speaker disagreements, 90 records with question IDs shared across records, and
54 nullable-field records (including optional categories). Anomaly categories
overlap. There are 182 agent-eligible and 181 step-eligible records. Raw values,
eligibility, nullable/missing fields, row provenance, omitted ranges, selected
sequences, truncation, and digests remain in the local per-record audit files.
The current aggregate is [evaluation-context-v2.json](evaluation-context-v2.json).

The [dataset card](https://huggingface.co/datasets/Kevin355/Who_and_When/blob/main/README.md)
defines two subsets in one train container each. The inspected paper and repository
prescribe no development/final partition. This project therefore freezes its own
grouped protocol; the final subset is not the paper's complete benchmark.

The manifest in [research/manifests/who_when-v1.json](../research/manifests/who_when-v1.json)
contains all sample identities and content digests. Grouping takes the transitive
union of equal question IDs or question text across subsets. Case folding and
whitespace collapsing apply only to conservative grouping, never scoring. Groups
exposed by existing committed test fixtures are reserved for development. A fixed
greedy coverage rule, with digest tie breaks and no randomness, selects 16
agent-eligible development cases across subsets, trace lengths, context modes,
agents, and anomaly presence before predictions exist. Related records stay
together: 35 development records and 149 final records. Extra development group
members are not automatically added to the 16-case paid sample. The sample contains
13 Algorithm-Generated and three Hand-Crafted records, 13 full-context and three
pruned-context records, and 16 distinct agent labels. All 16 have eligible step
annotations. This is a coverage-oriented development sample, not a population
accuracy estimate.

## Reproduce the audit and inspect a development plan

Run from the repository root with the existing Python/uv workflow. PyArrow 23.0.1
is the only added library, locked in the optional evaluation dependency group.
Production installation does not select that group.

```powershell
uv sync --locked --group evaluation
uv run --group evaluation python -m scripts.evaluate_attribution --context-only --output .local/evaluation/context-audit
uv run --group evaluation python -m scripts.evaluate_attribution --manifest research/manifests/who_when-v1.json --output .local/evaluation/attribution-plan
```

The audit processes all records without OpenAI or Docker. The second command is
also offline. Neither command fabricates predictions. Outputs default to exclusive
creation; use a fresh output directory or `--resume` with identical configuration.

## Explicit development inference

No paid Phase 8 development or final run was performed during implementation.
The real backend E2E documented elsewhere is not an attribution benchmark result.
The subsequent authorized recovery run completed the six live regression cases
and fixed 16-case Luna Medium attribution sample. See
[the development observations](phase8-development-results-v1.md) and their separate
machine-readable track reports. The subsequent
[final backend review](final-backend-pass.md) keeps both prompts, adds early harness
compatibility checks and safe diagnostics, and prepares the frozen configuration.
No final-set inference was performed.

After inspecting the audit and fixed development sample, the following command
opts into at most 16 calls, one per selected sample, using Luna Medium:

```powershell
uv run --group evaluation python -m scripts.evaluate_attribution --manifest research/manifests/who_when-v1.json --output .local/evaluation/luna-medium-dev --max-samples 16 --max-calls 16 --model gpt-5.6-luna --reasoning-effort medium --live
```

Re-run the identical command with `--resume` to continue. `--max-calls` is a total
experiment limit including previously started and failed calls. Increasing that
limit does not change the frozen sample selection. `--max-samples` defaults to 16;
omitting it never launches all 184. The runner prints the model, effort, planned
call upper bound, completed/remaining samples, and already started calls first.

Ground truth is consulted for scoring only after the prediction attempt. Positive
input projection admits only question/history; the existing adapter, context
selector, production request builder, provider boundary, and semantic validator
are reused. Incorrect answers and abstentions are retained without retries. SDK
retries remain disabled. Provider and stage failures are terminal observations.

## Results, resume, and cost accounting

Each output directory contains a frozen `config.json`, call intents and saved
responses/errors, per-sample JSON results, and immutable JSON/Markdown report
snapshots. Sample records link their experiment ID to model, effort, prompt version
and instruction digest, context policy, scoring version, normalized source digest,
timeout, and output limit. Input digests, predictions, evidence, raw labels,
eligibility, confidence, context statistics, correctness, safe failures, and
timestamps are preserved. No experiment tables or migrations are required.

The call intent is flushed before inference, and each response is saved before
scoring or subsequent stages. A resumed partial Track B sample reuses attribution
and synthesis responses. Completed sample attempts, including wrong answers and
errors, are never rerun. Usage aggregation scans the call journal, so a partial
sample's paid attribution is included even before synthesis completes. Unknown
usage remains null; network failures do not acquire invented token counts.

If interrupted after an intent but before a saved response, resume stops with an
uncertain-call diagnostic and makes no replacement call. Inspect that journal and
provider records before explicitly deciding how to proceed. A crashed `.lock`
must likewise be inspected and removed manually only after confirming no runner
is active. Corrupt files or changed configuration fail closed. Artifact files are
exclusive and fsynced; this is local research persistence, not a distributed job
queue or a guarantee against disk loss during a write.

Reports include exact agent/step/joint correctness with eligible denominators,
coverage, abstention rate, accuracy among covered predictions, confidence
distribution and accuracy, invalid semantic references, context/subset/truncation
breakdowns, exclusions, stage failures, latency, and token counts. Overall accuracy
counts abstentions and failed attempts as unsuccessful for each eligible metric.
Coverage and abstention use all terminal sample attempts; failures are reported
separately. Thus coverage + abstention + failure fractions sum to one. Confidence
breakdowns include valid abstentions; covered accuracy excludes them. No LLM judges
are used for scoring.

Input, output, reasoning tokens when supplied, response ID/model, and provider
latency come from the existing `ProviderUsage` boundary. Sample wall time is also
recorded for the current runner invocation. A resumed sample can reuse earlier
provider durations, which remain in the call journal; wall time is not cumulative
across interruptions. Responses do not supply billed dollar cost; reports leave it unknown and
do not assume a price. Reasoning tokens are a component of output usage, not an
extra quantity to add to total output tokens.

## Controlled comparison and final freeze

Inspect and stabilize the Luna Medium baseline before optional comparisons. Use
the same manifest, sample limit, prompt, context, scoring, and implementation with
a fresh output directory for each model/effort. No automatic prompt tuning or model
selection exists. A legitimate prompt change requires a new version and experiment;
old response files remain intact.

```powershell
uv run --group evaluation python -m scripts.evaluate_attribution --manifest research/manifests/who_when-v1.json --output .local/evaluation/luna-high-dev --max-samples 16 --max-calls 16 --model gpt-5.6-luna --reasoning-effort high --live
uv run python -m scripts.compare_evaluations --baseline .local/evaluation/luna-medium-dev --candidate .local/evaluation/luna-high-dev --output .local/evaluation/medium-vs-high.json
```

Comparison rejects incomplete baselines and changes to samples, prompts, context,
scoring, or source. A tiny sample does not justify an automatic costlier model choice.

Once the development results are inspected, freeze the selected configuration:

```powershell
uv run --group evaluation python -m scripts.freeze_evaluation --development .local/evaluation/luna-medium-dev-v1 --manifest research/manifests/who_when-v1.json --review research/reviews/attribution-development-review-v1.json --reviewed-development --output research/frozen/who_when-final-v2.json
```

The final command for the current 149-record holdout is below. It is supplied for
future explicit use and was not executed. It requires a reviewed freeze matching
the complete source/configuration and manifest. Changing source, model, effort,
prompt, context, or scoring requires another development/freeze cycle.

```powershell
uv run --group evaluation python -m scripts.evaluate_attribution --manifest research/manifests/who_when-v1.json --partition final --freeze research/frozen/who_when-final-v2.json --output .local/evaluation/final-luna-medium-v1 --max-samples 149 --max-calls 149 --model gpt-5.6-luna --reasoning-effort medium --live
```

Resume that exact command with `--resume`. Never use final results to revise the
prompt and then report another attempt on those records as untouched evaluation.

The current freeze format is `phase8-freeze-v2`; the runner is `phase8-runner-v2`.
It pins Parquet byte digests, logical dataset digest, the complete 149 IDs/manifest,
source digest/revision, output/timeout settings, and every inference/scoring version.
Freeze creation reconstructs every original development request, context, and score
without a provider. Code hardening with exact input/config/scoring equivalence can
retain the historical baseline; changed inference settings cannot. A final run rejects
a partial final manifest or any pin mismatch. A freeze authorizes no spending: the
live command still requires separate explicit user authorization. Existing output
directories retain their original version; do not resume v1 under changed source.
The current artifact is `who_when-final-v2.json`; v1 remains auditable and is superseded
only for final formatter fixes. Neither artifact has been used for final inference.

## Track B: controlled regression synthesis

The six developer-owned deterministic systems cover arithmetic, missing field,
unexpected field, required string content, forbidden content, and completion
status. Each really runs coordinator, target, and reporter agents. The same target
invocation contract and structured setup/stimulus are used for faulty and corrected
variants. No new assertion kind or framework was introduced. The arithmetic target
reuses the existing calculator implementations.

Task requirements in the recorded execution are ordinary evidence. Private oracle
metadata and corrected observations are not passed to either provider request.
Tests prove that modifying the private oracle leaves both prompts unchanged.
The runner invokes the existing attribution, RegressionSpec provenance validation,
compiler, assertion helpers, and validation verdict classifier directly. It is a
research runner for the phase logic; it does not benchmark HTTP/Celery throughput.

```powershell
uv run python -m scripts.evaluate_regressions --output .local/evaluation/regression-plan
uv run python -m scripts.build_evaluation_sandbox
```

The plan is offline and checks actual variants against private developer oracles.
The build command needs the existing verified `traceguard-validation:1` base image
and Linux Docker. It sends only trusted harness files as build context. The suite
overlay (`traceguard-evaluation:2`) has a separate image/bundle identity; it never replaces the production
image or production registry. Execution reuses the existing non-root, networkless,
read-only, resource-limited Docker transport, timeout, assertion provenance hook,
and cleanup. Generated Python is never executed on the host.

After inspection, opt into at most 12 calls for all six scenarios:

```powershell
uv run python -m scripts.evaluate_regressions --manifest .local/evaluation/regression-plan/suite-manifest.json --output .local/evaluation/regression-dev --max-samples 6 --max-calls 12 --model gpt-5.6-luna --reasoning-effort medium --live
```

Use identical arguments plus `--resume` to continue. Actual calls can be fewer
when attribution abstains or synthesis preflight requires review. Both base and
overlay image IDs are pinned before the first paid request and checked on resume.

Reports distinguish diagnosis success, synthesizable and needs-review rates,
compilation, `valid`, `not_reproduced`, `not_discriminating`, `inconclusive`, and
stage failures. Complete E2E success additionally requires the correct diagnosis,
a grounded spec, compiled artifact, faulty assertion failure, and corrected pass.
Every rate uses all terminal scenario attempts as its denominator. Inconclusive
sandbox outcomes cannot become valid regressions. Offline tests use labeled
fakes only as test fixtures, never as reported model experiments.

The replay contract is checked before compilation with `controlled-suite-replay-v1`,
shared with the actual harness invocation validator. Provenance-valid nested scalars
remain unchanged and are rejected when the harness requires a full mapping. Reports
add replay-compatible count/rate and explicit `replay_contract` failures. Both
historical prompts are preserved. The original live result remains three valid and
three inconclusive regressions; six passing deterministic Docker fixtures do not
change that model result.

## Verification and remaining work

```powershell
uv run --group evaluation pytest tests/unit/test_evaluation_data.py tests/unit/test_evaluation_experiments.py tests/unit/test_evaluation_regressions.py -q
uv run ruff check .
uv run mypy
$env:RUN_EVALUATION_SANDBOX_TESTS = "true"
uv run pytest tests/integration/test_evaluation_sandbox.py -q
Remove-Item Env:RUN_EVALUATION_SANDBOX_TESTS
```

The Docker suite check uses private test fixtures and no OpenAI calls. Normal
tests skip it without the explicit environment flag; normal CI installs the optional
Parquet group to exercise loading. Existing paid suites retain their separate flags.

Implementation verification on 2026-10-17 passed the offline application suite
(618 passed, 121 infrastructure-dependent tests skipped, two paid suites
deselected), and the expanded evaluation-specific tests are recorded in the final
verification artifact, [evaluation-verification.json](evaluation-verification.json).
Ruff and the existing app mypy target passed. Both research
CLIs were dry-run without provider calls.

Formatting passes for all added evaluation Python files. Repository-wide format
checking identifies four pre-existing differences in `app/api/health.py`,
`app/core/config.py`, `app/services/attribution_input.py`, and
`docs/pytest-compiler-v1.md`; these unrelated files remain untouched.

That paragraph records the initial tooling check. The final backend pass subsequently
formatted the modified config and the three remaining whitespace-only differences;
repository-wide formatting now passes. The frozen source change is documented as v2.

At implementation time, real Docker verification could not complete: both existing
Docker contexts returned HTTP 500 for daemon info/version, and preflight reported
unavailable resource-limit support. All six opt-in Docker cases failed before
generated execution; no valid regression verdict is claimed from that attempt.
The production sandbox policy was preserved. The subsequent recovery found both
contexts healthy on the same Linux daemon, built the verified overlay, and passed
all six fixture-based faulty/corrected checks. One Phase 7 control-command timeout
passed on an unchanged recheck. See the recovery report for this history and the
separate live model results: three valid regressions and three inconclusive pairs.

Paid development results are now recorded without tuning or retries. Final metrics
and claims about generalization remain future work. The six-case suite is small; repeated-run
stability and additional realistic systems can be studied later without interpreting
these fixtures as comprehensive coverage. Frontend dashboards, thesis charts, and
publication formatting remain outside this phase. The current deliverable is
auditable measurements and tooling, with no UI, Redis, or new production services.
