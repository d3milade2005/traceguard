# Phase 6: deterministic PyTest compilation

Phase 6 synchronously compiles a completed synthesizable RegressionRecord into a
statically checked GeneratedTest. It makes no LLM request and never runs or imports
generated source. No Celery task, template engine, or new dependency is needed: the
bounded source has one small fixed rendering layout.

## Input and compilation

The compiler consumes the unchanged `regression-spec-v1` contract and its server-created
`scenario_snapshot`. Review, failed, and incomplete records are ineligible. Missing or
invalid snapshots, unsupported versions/kinds, invalid paths, or inconsistent target
metadata fail closed. Raw contract fields are validated before serialization, preserving
booleans versus integers and refusing coercion of invalid union members. The service
also revalidates Phase 5 grounding and compares the snapshot with canonical evidence;
recorded output cannot become executable stimulus. Who&When remains an attribution
benchmark; its nonreplayable traces can remain `needs_review`.

`app.services.pytest_compiler.PytestCompiler` is pure and performs no persistence or
inference. Its render model contains only a validated test identifier, literal target,
canonical JSON snapshot, and typed assertions. JSON serialization sorts object keys,
uses compact separators and ASCII escapes, and refuses nonfinite numbers. Python string
literal escaping preserves quotes, backslashes, newlines, and Unicode as data. Each test
loads a fresh JSON fixture; precondition order is retained. Prose, UUIDs, times, and
provider metadata do not influence generated source.

Generated files contain a fixed header, `import json`,
`from app import regression_runtime as runtime`, and one function accepting the
`regression_harness` PyTest fixture. The body loads the literal scenario, checks the
harness identity, invokes it, validates the observation, and calls fixed assertion
helpers. There are no generated classes, decorators, dynamic entry points, network,
subprocess, filesystem, environment, or installation operations.

## Approved harness and observation

`app.regression_runtime.RegressionTestHarness` is the developer-owned boundary:

```python
identifier = "traceguard.agent-invocation"
version = "1"


def invoke(*, target_agent, stimulus, preconditions) -> RegressionObservation: ...
```

`target_agent` is a literal identity, never a module import. `stimulus` is the server's
complete JSON input/tool-argument value; `preconditions` is an ordered tuple of the
server-resolved JSON values. The corresponding source references remain in the
RegressionSpec. A developer implementation must explicitly support its targets and
scenario shape, reject unsupported data, and map preconditions to its own state.
Phase 6 defines the interface, not a framework adapter or production implementation.

`RegressionObservation` is a strict Pydantic model with only `output: JsonValue`,
`tool_result: JsonValue`, and `status: str | None`; each defaults to null. Root null
observables are present values. The harness must return this model, which is revalidated
without converting nonfinite numbers to null. A mismatched harness identifier/version
or malformed observation fails instead of selecting another entry point.

Phase 7 must provide the `regression_harness` fixture from an approved developer-owned
implementation in its isolated environment. The identifier names the invocation
contract; matching string attributes alone does not establish code provenance. Phase 7
must approve and pin the implementation and runtime helper package itself.

## Assertion mapping and paths

The Phase 5 validator and generated tests share one JSON resolver/evaluator. No assertion
semantics were added. Each kind maps to a fixed helper:

| Kind | Generated helper | Semantics |
| --- | --- | --- |
| `equals` | `runtime.assert_equals` | Present value equals the scalar with the same exact JSON scalar type |
| `not_equals` | `runtime.assert_not_equals` | Present value differs in type or value |
| `contains` | `runtime.assert_contains` | Present string includes the nonempty expected string |
| `not_contains` | `runtime.assert_not_contains` | Present string omits the nonempty expected string |
| `exists` | `runtime.assert_exists` | Path is present, including present-null |
| `not_exists` | `runtime.assert_not_exists` | Path is absent |

Equality distinguishes `true`, `1`, and `1.0`. All missing comparison operands fail,
including negated comparisons. Nonstring containment is invalid. Existence assertions
omit the expected operand; the validated IR requires it to be null. Helpers explicitly
raise on failure, retaining their behavior with optimized Python.

Paths contain literal validated string keys and nonnegative integer indexes. Resolution
traverses only exact built-in dicts/lists, with bounds checks. It never resolves attributes,
dotted expressions, descriptors, functions, custom mapping lookups, or boolean indexes.
An empty path selects the observable itself. Keys that look like attribute names remain
literal dictionary keys and cannot execute attributes.

## Static validation and determinism

Before persistence, source is parsed using `ast.parse`, without importing, executing,
or running the generated module. Checks enforce approved imports, one expected test
function, the fixture parameter, allowed AST nodes, approved calls/attributes, and exact
byte-for-byte agreement with the compiler's fixed layout. Nested unexpected imports,
dynamic calls, loops, classes, decorators, and source modifications are refused.
`GENERATED_TEST_MAX_SOURCE_BYTES` defaults to 262144; scenario limits reuse Phase 5's
UTF-8 byte bound. The source digest is lowercase SHA-256 of the exact UTF-8 bytes.

The same validated spec/snapshot and compiler/harness versions produce identical source,
including dictionary order independence. Lines use LF and contain no current timestamps
or random identifiers. Runtime/helper semantics and versions must remain pinned for
reproducible Phase 7 execution. Static validation is defense in depth, not a sandbox.

## Persistence, API, and errors

Migration `0005` adds only `generated_tests`. Columns include UUID, RegressionSpec
foreign key, artifact version, framework, test name, compiler/harness identity and
versions, source text/digest, safe failure code, and timestamps. The smallest synchronous
lifecycle has only terminal `completed` and `failed` states. Completed artifacts require
source, valid digest, test name, and completion time; failed records retain no source or
completion metadata. No execution or ValidationResult tables are introduced.

Uniqueness covers `(regression_spec_id, compiler_version, harness_identifier,
harness_version)`; status/creation is indexed. The service locks the parent during bounded
compilation and commits the terminal artifact atomically. Concurrent/repeated requests
return one identical artifact without another compilation. No distributed lock or broker
is involved. Existing failed operations are returned for inspection rather than reset.

- `POST /api/v1/regression-specs/{regression_spec_id}/generated-tests`: 201 and Location
  for a newly completed artifact; 200 with the existing completed/failed artifact on repeat.
- `GET /api/v1/generated-tests/{generated_test_id}`: artifact, source/digest, lifecycle,
  compiler/harness metadata, timestamps, and safe failure details.
- Unknown IDs return the existing consistent 404 error format. Ineligible input returns
  409 without creating an artifact. Unsupported version/kind, invalid IR, or invalid
  scenario returns 422 with a persisted failed artifact ID. Syntax, safety, or compiler
  invariant failures return 500 with the preserved failed artifact ID. Database failures
  return safe 503 and roll back the artifact transaction; an unavailable database cannot
  be relied on to record its own failure. No automatic repair, LLM fallback, or retries
  are introduced.

Apply `uv run alembic upgrade head` before starting the updated API. Stop application
writers before downgrading to `0004`; only GeneratedTest artifacts are removed.

## Exact Phase 7 input and remaining responsibilities

Phase 7 receives a `GeneratedTestRecord` with:

- `status="completed"`, `version="generated-test-v1"`, `framework="pytest"`;
- `compiler_version="pytest-compiler-v1"`;
- `harness_identifier="traceguard.agent-invocation"`, `harness_version="1"`;
- the exact `source_code`, `source_digest`, `test_name`, ID, and `regression_spec_id`;
- the linked completed synthesizable RegressionRecord, its validated spec, and its
  server-resolved scenario snapshot for provenance and fixture inspection. The same
  stimulus/preconditions are already embedded as literal JSON in the generated source.

Only compiler/runtime code and the explicitly approved developer harness implementation
are trusted components. The IR, fixtures, recorded observations, and generated Python
remain untrusted data/artifacts. Phase 7 must check digest and version compatibility,
pin the harness/runtime, and execute PyTest only inside an isolated environment with
timeouts, CPU/memory limits, filesystem/environment/network restrictions, and cleanup.
It must supply the approved fixture, then implement faulty/corrected validation and
validation-result persistence. None of this execution, comparison, repair, or sandbox
work is performed in Phase 6.

## Tests

Offline tests cover all six kinds, typed equality, null/missing values, containment,
nested/list paths, observation and harness contracts, literal serialization, deterministic
source/digest, input refusal, static syntax/safety checks, and execution/provider guards.
The developer-controlled `tests/fixtures/replayable.py` calculator is a genuine small
replayable system, independent of Who&When. Tests inspect generated text and AST only;
normal unit tests exercise trusted helpers and the fixture contract directly.

PostgreSQL tests cover synchronous API/store behavior, concurrent idempotency, canonical
snapshot consistency, safe failed operations, atomic rollback, foreign keys, uniqueness,
constraints, indexes, schema checks, and upgrade/downgrade preserving Phase 5 data.
Normal verification requires no OpenAI key or paid call.

## Implementation verification — 2026-10-02

The final verification of the full unit suite and Phase 6 PostgreSQL integration
suite passed: **521 tests** (508 unit, 13 integration). Ruff, formatting, mypy
(73 application modules), `git diff --check`, and `docker compose config --quiet`
passed. Integration checks exercised migration upgrade/downgrade, schema consistency,
foreign keys, constraints, concurrent idempotency, and atomic failure handling.

An earlier broader run passed 574 tests and skipped both opt-in live tests, but was
interrupted by PostgreSQL restarting: one test failed and six setup/teardown errors
reported lost connections or recovery mode. All affected Phase 6 tests passed in the
final rerun after recovery. The existing Starlette/AnyIO deprecation warning remains.
There were zero paid API calls and no generated source was executed or imported.

New files contain the pure compiler, shared runtime/harness contract, artifact schema
and model, synchronous service and routes, migration `0005`, controlled replayable
fixture, tests, and this guide. Existing application wiring, Phase 5 validation helper
composition, errors/logging, configuration example, migration expectations, and README
were updated. No dependency, historical migration, or `AGENTS.md` changed. Application
containers were not deployed; apply `0005` before starting the updated API.
