# Phase 8 Docker recovery and fixed development observations

Subsequent offline review and backend corrections are documented separately in
[the final backend pass](final-backend-pass.md). The measurements below and linked
v1 machine artifacts remain historical and unchanged; no paid rerun was performed.

Recorded 2026-10-17. Both tracks completed without changing prompts, model, reasoning effort, context, scoring, or manifests during inference. No final evaluation call was made. The configuration has not been frozen for final evaluation.

## Docker diagnosis and fix

Docker Desktop's Linux daemon was already responding when recovery inspection began. Both `desktop-linux` (active) and `default` now reach the same daemon. The historical HTTP 500 is no longer reproducible; available diagnostics do not establish its underlying cause. No daemon restart, context switch, API-version override, or global Docker settings change was performed. The agent's restricted process separately lacked access to Docker user configuration/named pipes; authorized Docker commands ran outside that process boundary.

A separate application security defect was confirmed: daemon info reported `name=seccomp,profile=unconfined`, and the previous preflight accepted any string containing seccomp. Validation containers now explicitly select `--security-opt seccomp=builtin`, and preflight requires the exact named seccomp capability. Docker 28.1.1 supports this explicit choice ([versioned implementation](https://github.com/moby/moby/blob/v28.1.1/daemon/seccomp_linux.go), [Docker run documentation](https://docs.docker.com/reference/cli/docker/container/run/)). A trusted real-container probe confirmed Seccomp=2, NoNewPrivs=1, and zero effective capabilities.

All other sandbox controls remain: Linux, UID/GID 10001, network none, read-only root, all capabilities dropped, no new privileges, 256 MiB memory and swap bound, one CPU, 32 PIDs, 16 MiB bounded noexec/nosuid/nodev tmpfs, and unchanged deadlines. Execution containers receive no host mounts, repository, credentials, or Docker socket. Generated Python ran only inside Docker. The trusted control-worker probe alone used the existing socket contract, with UID 10001 and supplemental group 0; it reached the same daemon without credentials. The persistent Celery worker was not started; its prior logs showed broker connection failures, which are distinct from Docker transport.

Engine/client 28.1.1, API 1.49, Docker Desktop 4.41.2, Linux/amd64 on WSL2 kernel 6.6.114.1. Actual image versions: Python 3.12.12 and PyTest 9.1.1. The approved production image did not need rebuilding. The minimal trusted six-scenario overlay was built and verified against that base.

- Base image: `traceguard-validation:1`, `sha256:7a4b51cdd46e1e40dab633f0ad54c182c90ba7c9c4fef3f2ab6f6019d0f6bc32`.
- Approved base bundle: `580d2021e1f086decd7e5fc9f090b2bae68363dd3d4097bdd0f10ff9cecb4731`.
- Evaluation image: `traceguard-evaluation:1`, `sha256:eca56a831d45065cb40daf547dfd8b02ec1812fe5c83497c2af2ba458bcf3904`.
- Evaluation bundle: `93e626b4f6e1a026dfe061d2535009790ca0d765efc8c305a5d3b68d2598911c`; developer-owned harness `controlled-suite-v1`.

Full safe daemon/image observations: [evaluation-docker-recovery-v1.json](evaluation-docker-recovery-v1.json). Local Docker test XML remains in `.local/evaluation/recovery-docker-tests.xml` and `recovery-docker-recheck.xml`.

## Track B: six controlled live regression experiments

All six developer-fixture Docker checks passed before paid inference. These checks prove the harness/compiler/sandbox can discriminate the known variants; they are not model results. The separate live experiment used Luna Medium, at most 12 calls (exactly 12 used), `attribution-v1`, `regression-spec-v1`, `phase3-structural-v1`, and `controlled-oracle-exact-v1`. Private oracles and corrected observations were not model inputs.

Experiment ID: `87d31581c4358aff48e816c433744faedcd1e6d125286d08d8b2f1951efdf3f2`.

| Scenario | Predicted agent @ step | Spec | Compiled | Faulty | Corrected | Verdict | Total wall seconds |
| --- | --- | --- | --- | --- | --- | --- | ---: |
| arithmetic | calculator @ 1 (correct) | synthesizable | yes | assertion_failed | passed | valid | 22.782 |
| missing-field | profile_builder @ 1 (correct) | synthesizable | yes | assertion_failed | passed | valid | 17.516 |
| unexpected-field | public_formatter @ 1 (correct) | synthesizable | yes | assertion_failed | passed | valid | 35.859 |
| incorrect-string | greeter @ 1 (correct) | synthesizable | yes | execution_error | execution_error | inconclusive | 17.266 |
| forbidden-content | redactor @ 1 (correct) | synthesizable | yes | execution_error | execution_error | inconclusive | 16.875 |
| missing-completion | completion_agent @ 1 (correct) | synthesizable | yes | execution_error | execution_error | inconclusive | 13.343 |

Attribution success 6/6 (100%); synthesizable 6/6 (100%); needs review 0/6 (0%); compiled 6/6 (100%); valid regressions and full E2E success 3/6 (50%). Not reproduced 0/6; not discriminating 0/6; inconclusive 3/6 (50%). No provider/stage exception occurred. Execution errors in both variants are reported as inconclusive, never valid.

The three inconclusive cases (`incorrect-string`, `forbidden-content`, `missing-completion`) selected setup `output.mode` at sequence 0, yielding the scalar `public`. The harness contract requires the full setup mapping `{'mode': 'public'}`. The compiled fixture therefore supplied unsupported preconditions and both variants returned execution_error before the behavioral assertion. Successful specs selected the whole setup. This is an observed synthesis/harness compatibility limitation. Ground truth, harness, prompts, and results were not changed to improve the result; no case was retried.

[Machine-readable Track B report, all sample records, and call journal](evaluation-regression-dev-v1.json). Compiled artifact JSON and validation pairs remain in `.local/evaluation/regression-luna-medium-v1/artifacts` and `validation`.

## Track A: frozen 16-case attribution development sample

Exactly the existing 16 development sample IDs were used, with 16 calls and a 16-call cap. The 149-record final set received no inference. Runtime: `gpt-5.6-luna`, reasoning `medium`, prompt `attribution-v1`, context policy `phase3-structural-v1`, scoring `strict-exact-no-agent-normalization-v1`, semantics `whowhen-official-zero-based-v1`. Provider responses also identify the model as gpt-5.6-luna. Timeout was 60 seconds and maximum output was 4096 tokens, unchanged throughout. Ground truth was loaded for scoring after prediction attempts. No labels were supplied to inference.

Experiment ID: `08bb6be41c9c675a4da60f18decf32319039e529c955753f61f81487be215660`. Both tracks pin source digest `6fb425284cd2f758dfb0fdb433b88158b4bd59bf4fdb45e0ae8c605e52af8a16`. Split-manifest digest: `1b6927dcdc7ae0118a18f6aa917369a1fa30b0b9689a204e3769d6e1db060a76`.

| Metric | Result |
| --- | ---: |
| Responsible-agent accuracy | 9/16 (56.25%) |
| Decisive-step exact match | 6/16 (37.50%) |
| Joint accuracy | 6/16 (37.50%) |
| Abstention | 1/16 (6.25%) |
| Coverage | 14/16 (87.50%) |
| Agent accuracy among covered predictions | 9/14 (64.29%) |
| Step/joint accuracy among covered predictions | 6/14 (42.86%) |
| Structured-output failures | 1/16 (6.25%) |
| Invalid semantic references after parsing | 0/16 (0%) |

All 16 annotations are agent-, step-, and joint-eligible. Overall accuracies retain the abstention and failed attempt in their denominators. Coverage 87.50% + abstention 6.25% + failure 6.25% = 100%. A failure is not an abstention. High confidence occurred 14 times (including the abstention), medium once, low zero times; the failed attempt has no confidence. Agent accuracy across high-confidence outputs is 9/14 (64.29%), and among covered high-confidence outputs 9/13 (69.23%). The single medium-confidence attribution was incorrect. There are no low-confidence observations from which to infer error correlation.

Sample IDs below abbreviate the stable content digest to 12 characters. The machine-readable report preserves each full subset/digest/occurrence identity and original labels. AG = Algorithm-Generated; HC = Hand-Crafted. Steps are zero-based positions in the entire original history, including human/system messages. A/S/J means agent/step/joint exact-match correctness; these measure benchmark agreement, not a separate causal-explanation judgment.

| Sample digest | Subset | Ground-truth agent | Predicted agent | GT step | Predicted step | Confidence | Abstained | Context | A/S/J |
| --- | --- | --- | --- | ---: | ---: | --- | --- | --- | --- |
| 0d8b79c04fad | HC | Assistant | none | 16 | - | high | yes | full | no/no/no |
| db9ef45be133 | AG | PythonDebugging_Expert | PythonDebugging_Expert | 0 | 0 | high | no | pruned | yes/yes/yes |
| 3c12f235ac81 | AG | HistoricalWeatherData_Expert | Statistics_Expert | 4 | 6 | high | no | full | no/no/no |
| 3c310f4a7a83 | AG | WebDevelopment_Expert | output failure | 1 | - | - | no | full | no/no/no |
| 0c3988a06834 | HC | Orchestrator | WebSurfer | 14 | 28 | medium | no | pruned | no/no/no |
| bd19293a21a5 | HC | WebSurfer | Orchestrator | 32 | 129 | high | no | pruned | no/no/no |
| 568fafbf10ab | AG | InformationVerification_Expert | DataAnalysis_Expert | 6 | 5 | high | no | full | no/no/no |
| 5e0abb29aec1 | AG | Verification_Expert | Verification_Expert | 1 | 3 | high | no | full | yes/no/no |
| 44a116c0d33f | AG | Validation_Expert | Validation_Expert | 5 | 5 | high | no | full | yes/yes/yes |
| 3b02aa5d15e6 | AG | Cubing_Expert | Cubing_Expert | 1 | 1 | high | no | full | yes/yes/yes |
| 5a1a837b70b7 | AG | InformationExtraction_Expert | InformationExtraction_Expert | 8 | 5 | high | no | full | yes/no/no |
| 2acac885ce30 | AG | DataVerification_Expert | DataVerification_Expert | 3 | 3 | high | no | full | yes/yes/yes |
| 88aa69b51f77 | AG | AlgorithmDesign_Expert | AlgorithmDesign_Expert | 4 | 4 | high | no | full | yes/yes/yes |
| 0db49775da30 | AG | Locomotive_Expert | Locomotive_Expert | 4 | 4 | high | no | full | yes/yes/yes |
| 832f164cdecd | AG | WaybackMachine_Expert | WaybackMachine_Expert | 1 | 6 | high | no | full | yes/no/no |
| 917ea0918e79 | AG | ModelEvaluation_Interpretation_Expert | Physics_Expert | 4 | 2 | high | no | full | no/no/no |

The failure is sample `Algorithm-Generated/3c310f4a7a83b8b3b82a107311b9ce8657cb81f75f7b95d0562bf1a6b761c85c/0`: `LLM_OUTPUT_INVALID` / `LLMOutputError` at attribution_provider. A response was received with usage, but its text did not validate against the attribution contract. The safe journal does not retain the rejected response body or specific Pydantic detail, so the precise contract violation cannot be established from these artifacts. No transport, timeout, quota, or refusal failure was observed. This paid attempt remains terminal and was not retried.

## Attribution context and subset observations

| Group | Cases | Agent accuracy | Step/joint accuracy | Abstentions | Coverage |
| --- | ---: | --- | --- | --- | --- |
| Full context | 13 | 8/13 (61.54%) | 5/13 (38.46%) | 1/13 | 11/13 |
| Pruned context | 3 | 1/3 (33.33%) | 1/3 (33.33%) | 0/3 | 3/3 |
| Algorithm-Generated | 13 | 9/13 (69.23%) | 6/13 (46.15%) | 0/13 | 12/13 |
| Hand-Crafted | 3 | 0/3 (0.00%) | 0/3 (0.00%) | 1/3 | 2/3 |

Decisive-step retention is 15/16 (93.75%): full 13/13, pruned 2/3; Algorithm-Generated 13/13, Hand-Crafted 2/3. One annotated step was omitted, none truncated. Sample `bd19293a21a5` lost annotated step 32 and predicted step 129 incorrectly. This is an observation, not proof that pruning caused the error. No evidence-truncation/abstention relationship can be measured because no selected sequence was truncated. Mean original context was 35,633.19 UTF-8 bytes and selected context 20,359.31; selected/original ratio was 0.5714. These byte estimates are distinct from provider token counts.

Three Hand-Crafted records have nullable optional mistake_type annotations; they remain fully scoring-eligible. The fixed sample contains no out-of-range step exclusions, agent casing exclusions, or speaker-disagreement exclusions. Raw labels were preserved without correction. The broader 184-record audit and frozen 35-development/149-final grouped split remain unchanged. This coverage-oriented sample is too small and unrepresentative for a final benchmark claim; subset and context composition are confounded.

[Machine-readable Track A report, all sample records, and call journal](evaluation-attribution-dev-v1.json). The original immutable experiment remains at `.local/evaluation/luna-medium-dev-v1`; a completed resume used zero additional calls (16 intents before and after).

## Usage and latency

| Track | Calls | Input tokens | Output tokens | Reasoning tokens | Total provider seconds | Mean provider seconds/call |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Regression | 12 | 12,235 | 4,734 | 2,555 | 70.157 | 5.846 |
| Attribution development | 16 | 98,483 | 9,203 | 6,219 | 141.876 | 8.867 |

All 28 calls supplied usage, including the rejected attribution output. Reasoning tokens are included within output tokens and must not be added again. Dollar cost is unknown; no price or billing estimate was invented. Per-sample wall durations and per-variant Docker durations are separate from provider latency and include control-plane overhead.

## Verification, changes, and remaining review

Targeted evaluation/validation tests: 109 passed. Normal offline suite: 622 passed, 121 infrastructure-dependent skipped, two paid suites deselected. Docker opt-in checks: first attempt 14 passed and one Docker inspect control timeout; that single probe passed on recheck with unchanged limits. All six fixture cases passed on their first healthy-daemon attempt. The DB-backed persistence case was deselected because no dedicated TEST_DATABASE_URL was configured. No paid tests ran during normal test discovery.

Ruff check passed; formatting passed for all 24 evaluation/recovery Python files; mypy passed its existing 96-file app target; git diff --check and Docker Compose validation-profile config passed. The repository-wide formatting differences documented previously remain outside this recovery. The completed attribution resume verified zero additional calls and reused incorrect answers, the abstention, and the failed attempt.

Recovery changes: `app/integrations/execution/docker_runner.py`, `tests/unit/test_validation.py`, `docs/isolated-validation-v1.md`, `docs/phase8-evaluation.md`, this report, and the three linked observation JSON files. Existing Phase 8 implementation changes remain in the working tree. No migrations, dependencies, assertion DSL, production prompts, model settings, context policy, scoring rules, split manifests, frontend, Redis, or new HTTP endpoints were changed in this recovery.

The baseline is complete and ready for manual inspection. I would defer freezing the final configuration until the three synthesis/harness compatibility failures and the attribution contract failure have been reviewed. Any later implementation or prompt change requires a separately versioned experiment; these historical results remain immutable. No tuning, stronger-model comparison, automatic final freeze, or final benchmark was performed. The 149-record final set was NOT run. Frontend and thesis presentation remain future work.

Commands used for the paid development runs (shown for audit; completed outputs require --resume and do not repeat completed attempts):

```powershell
uv run python -m scripts.evaluate_regressions --manifest .local/evaluation/regression-recovery-plan/suite-manifest.json --output .local/evaluation/regression-luna-medium-v1 --max-samples 6 --max-calls 12 --model gpt-5.6-luna --reasoning-effort medium --live
uv run --group evaluation python -m scripts.evaluate_attribution --manifest research/manifests/who_when-v1.json --output .local/evaluation/luna-medium-dev-v1 --max-samples 16 --max-calls 16 --model gpt-5.6-luna --reasoning-effort medium --live --resume
```

The local invocation used the existing `.venv/Scripts/python.exe` directly. The attribution output was initialized offline before its first live invocation, hence --resume. The checked-in Who&When manifest was unchanged.
