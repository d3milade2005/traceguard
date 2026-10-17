# Phase 7: isolated faulty/corrected regression validation

Validation is an explicit asynchronous operation. It makes zero LLM requests, never
repairs source or specifications, and executes generated Python only in fresh Linux
Docker containers. API and ordinary Celery workers have no Docker runner or socket.

## Contracts and decisions

`ValidationRecord` links to GeneratedTest and has separate lifecycle and verdict.
Lifecycle is `pending -> queued -> processing -> completed / failed`. A completed
operation has both bounded execution summaries and a verdict. A failed operation
has a safe failure code; it does not claim a successful validation.

Each `ExecutionResult` records variant, outcome, exit code, duration, pass/fail/error
counts, timeout/resource/output-limit flags, container ID, and an allowlisted diagnostic.
No generated source, raw stdout/stderr, JUnit messages, fixture data, or secrets are
duplicated in validation results. Outcomes are `passed`, `assertion_failed`,
`execution_error`, `timeout`, and `resource_limit`.

| Faulty | Corrected | Verdict |
| --- | --- | --- |
| Expected regression assertion failure | Pass | `valid` |
| Pass | Pass or expected assertion failure | `not_reproduced` |
| Expected assertion failure | Expected assertion failure | `not_discriminating` |
| Execution error, timeout, or resource limit on either side | Any | `inconclusive` |

Both variants always receive the same exact artifact bytes. A timeout or deterministic
test error completes as inconclusive and is not automatically retried. Container
configuration/startup rejection fails the operation safely; daemon/database outages
may receive bounded infrastructure retries. An infrastructure failure before a complete
pair does not manufacture execution summaries.

## Verification and trusted registry

Before any execution, submission and processing verify completed `generated-test-v1`,
PyTest framework, `pytest-compiler-v1`, approved harness ID/version, nonempty source,
exact UTF-8 SHA-256 digest, linked completed synthesizable `regression-spec-v1`, valid
server scenario snapshot, and consistent artifact metadata. Processing checks that
these identities still agree with the operation created at submission.

Phase 6 AST/import/call restrictions remain. Phase 7 constructs a reference AST from
trusted fixed scaffolding and validated literal operands, and compares it with the
persisted source AST, including fixture, target, function, and assertions. It does not
regenerate, rewrite, import, or execute source during verification. Syntax or fixture
mismatch fails closed even if somebody changes the persisted digest too.

The explicit registry maps only `("traceguard.agent-invocation", "1")` to the
developer-owned `controlled-calculator-v1` pair: `FaultyCalculator` and
`CorrectedCalculator`. The input must target `calculator`, have integer left/right
operands, and use the supported add precondition. Faulty returns left + right + 1;
corrected returns left + right. Both return the Phase 6 RegressionObservation.
Other targets/scenario formats are rejected. Who&When is not given a fabricated
corrected implementation and remains an attribution benchmark.

Module/class/file paths, commands, images, PyTest options, and variant selection never
come from GeneratedTest or RegressionSpec values. Only the developer registry, pinned
image, fixed fixture, and trusted configuration determine execution. String identities
alone do not establish code provenance.

## Sandbox and runtime

`validation.Dockerfile` contains only the regression runtime helper, approved calculator
code, trusted PyTest fixture/hook, supervisor, and minimal locked dependencies. Python
3.12.12 uses a digest-pinned official base; PyTest 9.1.1 and dependencies use exact
versions and wheel hashes derived from `uv.lock`. The trusted bundle hash covers runtime,
harness, hook, supervisor, and dependency lock bytes. The worker checks runtime/bundle
labels and image configuration, resolves its trusted configured reference to an immutable
image ID, and uses that same ID for both executions. Repository digest is recorded when
available; locally built images are identified by immutable SHA-256 image ID.

Runtime identity includes versions, bundle digest, harness implementation, image
reference/ID/digest, and exact resource limits. Labels are a consistency check on an
image built by a trusted developer; they are not signatures or proof against a hostile
image publisher/daemon. Deploy an approved immutable image reference and protect its
build pipeline and Docker configuration.

Each container enforces:

- `--network none`, user `10001:10001`, read-only root filesystem;
- all capabilities dropped, no-new-privileges, explicit `seccomp=builtin`;
- no privileged mode, host bind mounts, repository, credentials, or Docker socket;
- 256 MiB memory with equal memory-swap bound, 1 CPU quota, 32 PIDs by default;
- a 16 MiB `/tmp` tmpfs with noexec/nosuid/nodev, no shared IPC;
- zero core dump limit, bounded open files, disabled Docker logging;
- a 10-second test deadline and an outer attach deadline with 15 seconds of grace.

The worker fails closed when the daemon is not Linux or reports missing memory, PID,
CPU quota, or seccomp support. Limits are centralized in Settings, exposed through
`VALIDATION_*` variables, and captured in runtime identity. Command operations have a
10-second timeout. Output and JUnit limits default to 32768 bytes each; source retains
Phase 6's `GENERATED_TEST_MAX_SOURCE_BYTES` bound.

The built-in seccomp profile is selected explicitly for every execution container.
It does not inherit an administrator's potentially unconfined daemon default.
Docker Engine 28.1.1 under Docker Desktop was verified with the Phase 7 probes on Windows;
an engine that rejects this option fails container creation without relaxing policy.
See [the recovery observations](phase8-development-results-v1.md) for daemon,
image, runtime-version, and non-root worker socket checks.

No host filesystem is mounted. Exact source bytes travel through stdin and become
`/tmp/bundle/test_generated.py` with read-only file permissions. This file is owned by
the sandbox user: chmod alone is not an immutability boundary. The externally persisted
artifact remains unchanged; static checks and container isolation remain necessary.
Only the container's bounded temporary filesystem is writable.

The trusted supervisor invokes a fixed `python -m pytest` command for this one file,
with fixed JUnit path, configuration, disabled third-party plugin discovery and cache,
and minimal environment. No shell or data-derived arguments are used. PyTest executes
in a child inside the sandbox, while the supervisor drains and discards bounded stdout
and stderr. Exceeding either stream's limit kills the child and records an output error.
The child also has an independent timeout. The outer Docker client is bounded and
force-removes the container when it returns or times out.

The supervisor emits only a bounded machine envelope and JUnit XML. The worker validates
types and byte bounds, rejects XML declarations with DTD/entity constructs, requires
exactly the expected test case, and checks exit code/count coherence. Arbitrary raw
stdout is never accepted as structured validation evidence. Failure details are
discarded before persistence.

The pinned hook marks a call failure as a regression assertion only when its exception
is AssertionError and its terminal traceback frame is the pinned runtime's `_assert`.
Harness exceptions, malformed observations, fixture/import/syntax failures, ordinary
assertions, PyTest internal errors, skips, and malformed reports cannot produce `valid`.
OOM/container SIGKILL is detected where available; other PID/filesystem/resource
failures may appear as execution errors and remain inconclusive.

## Persistence, ownership, queue, API

Migration `0006` adds only `regression_validations`, with GeneratedTest FK/cascade,
unique `(generated_test_id, validator_version)`, status/creation index, lifecycle,
claim, attempts, digest, outcome, and verdict consistency checks. Queryable identity,
lifecycle, verdict, and both outcomes are relational; the two small execution summaries
and runtime identity are bounded structured JSONB. Generated source is not duplicated.
Both summaries and the verdict commit atomically. Downgrade to `0005` removes only
validations and preserves GeneratedTest/RegressionSpec and earlier data.

- `POST /api/v1/generated-tests/{generated_test_id}/validations`: verify eligibility,
  persist/commit pending operation, publish its ID, then return 202 and Location.
  Repeat returns 200 and the existing operation, including terminal failure.
- `GET /api/v1/validations/{validation_id}`: lifecycle/verdict, both summaries,
  source/compiler/harness/runtime identity, timestamps, and safe failure code.
- Unknown IDs return 404; invalid/incompatible artifacts return 422; publication or
  persistence failure returns safe 503. Publication failures retain the failed ID.

Only `app.workers.validation_celery_app` registers `traceguard.validate_regression`.
The dedicated durable queue is `traceguard.validation`; ordinary apps register only
normal tasks/queues. Tasks compose the service and runner and stay thin. JSON messages
carry operation IDs and correlation headers, never executable options or source.

PostgreSQL row locks serialize submission and expiring UUID claims fence processing.
Duplicate active delivery waits; terminal delivery is a no-op; stale tokens cannot
commit results or release a newer owner. The lease exceeds bounded execution and task
timeouts. Up to three durable claims and two delayed infrastructure retries bound
reexecution; busy delivery has six bounded delayed retries. Crash recovery can repeat
work and does not promise exactly-once external execution. There is no outbox or
automatic stranded-operation scheduler. Republish a preserved pending/queued/expired
operation by its same ID when needed:

```sh
uv run celery -A app.workers.validation_celery_app:celery_app call traceguard.validate_regression --kwargs '{"validation_id":"<uuid>"}'
```

Container removal is in `finally`, including uncertain creation acknowledgements,
execution errors, timeout, malformed result, and database failure after execution.
There are no execution networks, volumes, or host execution directories to clear.
Containers have managed/operation/claim/expiry labels. A new claimed operation removes
expired managed containers and containers from previous claims of that operation.
After a worker kill or unavailable daemon, cleanup is eventual rather than guaranteed
immediate. Run the narrow cleanup command once Docker is available:

```sh
uv run python -m app.integrations.execution.cleanup
```

Logs allow only correlation IDs, variant/container ID, duration, lifecycle/verdict,
and safe codes. They do not include source, fixtures, JUnit, or stdout/stderr.

## Local setup and operations

Apply migrations before starting the updated API or worker:

```sh
uv run alembic upgrade head
```

Build the approved image from the repository root. PowerShell:

```powershell
$bundleHash = uv run python -c "from app.integrations.execution.registry import bundle_digest; print(bundle_digest())"
docker build -f validation.Dockerfile --build-arg "BUNDLE_DIGEST=$bundleHash" -t traceguard-validation:1 .
docker compose --profile validation up --build -d validation-worker
```

The validation worker's explicit opt-in Compose profile alone mounts Docker's socket.
It receives only database/broker and validation configuration, not `.env` or OpenAI
credentials. On Linux set DOCKER_GID to the daemon socket's group. Docker Desktop uses
a Linux VM and typically group 0 for its socket. This group/socket access grants
host-level Docker authority despite a non-root worker; it is an administrative trust
boundary, not a sandbox. Use a dedicated runner VM/host and restricted broker access
for deployments that process untrusted submissions. API and normal worker do not get
that authority. Worker build stage alone includes the Docker CLI.

A host validation worker can connect to the same local Docker daemon. Native Windows
Celery prefork is unsupported; use Linux/container workers for deployment. Solo mode
can be used locally, but cannot enforce Celery's process hard limits: the Docker client
and container supervisor deadlines still apply. Docker isolation shares a kernel with
its daemon host and is not proof against kernel/container escape vulnerabilities or a
compromised trusted image, daemon, runner, or PyTest runtime. It is not a general-purpose
sandbox or a substitute for those administrative boundaries.

## Verification and Phase 8

Normal tests use a fake runner without LLM or Docker calls. PostgreSQL tests use
dedicated `_test` databases and disposable schemas; RabbitMQ tests exercise the real
dedicated queue with a fake runner. Real Docker tests require explicit opt-in and the
approved image:

```powershell
$env:RUN_SANDBOX_TESTS = "true"
$env:RUN_LIVE_LLM_TESTS = "false"
$env:RUN_LIVE_REGRESSION_TESTS = "false"
uv run pytest tests -q
Remove-Item Env:RUN_SANDBOX_TESTS
```

Security probes contain fixed developer-owned Python that bypasses compiler eligibility
solely to exercise isolation. They execute only in the same hardened Docker runner.
Tests cover actual faulty assertion failure/corrected pass with persisted valid verdict,
all verdicts, static mismatches, ordinary assertion versus regression provenance,
harness/observation errors, timeout, oversized output, non-root, network, filesystem,
host data/socket absence, seccomp/capabilities, memory/PID/tmpfs limits, cleanup,
duplicate delivery, stale ownership, retries, atomic rollback, migrations, and schemas.
CPU quota configuration/support is checked; precise cross-platform CPU scheduling
performance is not asserted. Tests do not prove resistance to kernel exploits.

Phase 8 can build evaluation/scoring experiments from these auditable persisted
results and genuine replayable pairs. No benchmark scoring, dashboards/frontend,
model comparison, repair, regeneration, Redis, or provider call is implemented here.

## Implementation verification — 2026-10-02

A clean complete run passed **648 tests**, with **2 opt-in paid live tests skipped**.
It included the full unit, PostgreSQL, RabbitMQ/Celery, and Linux Docker sandbox suites.
The real controlled end-to-end operation persisted `status="completed"`,
`verdict="valid"`, faulty `assertion_failed`, and corrected `passed`, using the same
source digest and immutable image ID for both variants. No paid API calls were made.

Real security probes passed for non-root identity, read-only root, writable bounded
tmpfs, external-network denial, absent host repository/secrets/socket, no-new-privileges,
seccomp/capabilities, bounded output, timeout, memory termination, and process limits.
Tmpfs exhaustion was checked by an ENOSPC allocation probe that releases its own file
before PyTest emits JUnit. Sandbox settings were not relaxed.

Ruff, formatting, mypy (88 application modules), `git diff --check`, Compose configuration
including the validation profile, migration upgrade/downgrade, and schema consistency
passed. The existing Starlette/AnyIO deprecation warning remains.

Both the sandbox image and dedicated worker image built successfully. The packaged
worker bundle hash matched the host's trusted bundle, and its pinned Docker CLI
reported version 28.1.1. No generated code was used in the worker-image checks.

Docker reported zero remaining managed validation containers; temporary test schemas, including an old
interrupted-run schema, were removed. The persistent dedicated test database and any
existing seed/audit data were preserved. No execution networks, volumes, or host
execution directories were created. Approved images remain available for local use.

Changes add validation schemas/model/service, static artifact verification, explicit
harness registry/pair, narrow Docker runner and cleanup command, pinned sandbox image
and lock, dedicated worker/task/queue, API routes, migration `0006`, tests, and this guide.
Existing wiring, errors/logging/configuration, Docker/Compose, migration expectations,
and README were updated. Historical migrations and AGENTS.md were unchanged.
Application migration/deployment was not performed; apply `0006` and follow setup above
before using the new endpoints with an updated running worker.
