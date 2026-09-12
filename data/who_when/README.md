Who&When Dataset

This directory contains the local copy of the Who&When benchmark used in this project for failure-attribution research and evaluation.

The dataset is used to study whether the system can identify:

the responsible agent in a failed multi-agent execution; and

the decisive failure step where the error was introduced.

The full benchmark data is kept local and is not committed to Git. Small JSON fixtures derived from the dataset are stored separately under tests/fixtures/who_when/ for adapter and normalization tests.

Directory Structure

data/
└── who_when/
    ├── Algorithm-Generated.parquet
    ├── Hand-Crafted.parquet
    └── README.md

tests/
└── fixtures/
    └── who_when/
        ├── algorithm_generated_sample.json
        └── hand_crafted_sample.json

The local dataset currently contains:

Dataset subset

Records

History length

Algorithm-Generated

126

5–10 messages

Hand-Crafted

58

5–130 messages

Total

184

—

There are no local train/validation/test partitions in these files.

Dataset Fields

The two subsets share most of their structure, but there are some differences.

Field

Algorithm-Generated

Hand-Crafted

Meaning

history

List of messages

List of messages

Execution history

question

String

String

Original task

question_ID

String

String

Benchmark question identifier

ground_truth

String

—

Expected task answer

groundtruth

—

String

Expected task answer

is_correct

Boolean

—

Benchmark correctness flag

is_corrected

—

Boolean

Benchmark correctness-related flag

mistake_agent

String

String

Annotated responsible agent

mistake_step

String

String

Annotated decisive failure step

mistake_reason

String

String

Free-text failure explanation

mistake_type

—

Nullable string

Optional failure category

Benchmark labels such as mistake_agent, mistake_step, and mistake_reason must never be inserted into attribution inputs. They are evaluation ground truth.

Execution History Representation

Algorithm-Generated

Each history message contains:

{
  "content": "...",
  "name": "...",
  "role": "..."
}

Agent identity is primarily represented by name.

The role field may be assistant or user, but user does not necessarily mean a human user in this subset.

Hand-Crafted

Each history message contains:

{
  "content": "...",
  "role": "..."
}

The speaker is represented through role.

Examples include:

human

WebSurfer

FileSurfer

Assistant

ComputerTerminal

decorated Orchestrator roles

A value such as:

Orchestrator (-> WebSurfer)

means the Orchestrator is the speaker and WebSurfer is the recipient.

Canonical Trace Mapping

The production system must not depend directly on the Who&When schema.

Who&When records are converted through WhoWhenTraceAdapter into the project's framework-independent canonical trace representation.

The initial mapping is:

Canonical field

Mapping

sequence

Original history list position, starting at 0

agent_id

Algorithm-Generated: name; Hand-Crafted: normalized speaker from role

event_type

message

output

Original message content

input

None

status

None

tool_name

None

tool_arguments

None

tool_result

None

occurred_at

None

metadata

Original role and supported parsed role information

The original ordering must always be preserved.

Empty message bodies must also be preserved because removing them would shift later step indices.

Trace-Level Context

question is treated as trace-level task context.

It should not be inserted as an artificial execution step.

The trace source should be recorded as:

who_when

Benchmark-Only Fields

The following fields are evaluation metadata and must remain outside the canonical trace steps:

question_ID

subset identity

ground_truth

groundtruth

is_correct

is_corrected

mistake_agent

mistake_step

mistake_reason

mistake_type

dataset row/file provenance

These values should eventually be handled by the evaluation layer.

They must not be placed in:

canonical trace metadata;

Run.failure_signal;

attribution prompts; or

other model inputs used to predict the failure location.

This prevents benchmark ground-truth leakage.

Known Dataset Edge Cases

The dataset contains several important irregularities that should be preserved rather than silently corrected.

Observed examples include:

some responsible-agent labels disagree with the speaker at the annotated step;

some Hand-Crafted mistake_step values fall outside the corresponding history length;

Websurfer and WebSurfer both appear in labels;

some Algorithm-Generated messages have empty content;

question_ID values are not globally unique across the two subsets;

Hand-Crafted histories may contain OCR text, <Image> markers, and very large message bodies;

tool activity is sometimes embedded in text rather than represented as structured tool-call fields.

Do not silently:

shift labels;

clamp invalid indices;

rewrite annotation values;

remove empty messages; or

invent structured tool information that the source data does not contain.

Canonical step ordering should always come from the actual history list, not from mistake_step.

The exact interpretation of benchmark attribution labels belongs to the evaluation layer.

Test Fixtures

Small JSON fixtures are stored in:

tests/fixtures/who_when/

These fixtures exist so that:

adapter unit tests remain fast;

CI does not require the full benchmark dataset;

tests can verify real Who&When-shaped inputs.

The fixture records should preserve the original dataset structure.

Do not manually convert them into the canonical trace format before testing, because the purpose of the adapter tests is to verify that real Who&When-shaped data can be converted correctly.

The full dataset remains the source for research experiments and final evaluation.

Git Policy

The full benchmark files are intentionally excluded from Git.

The root .gitignore should include:

# Full local benchmark dataset
data/who_when/*.parquet

README.md should remain tracked.

The small fixtures under:

tests/fixtures/who_when/

should also remain tracked.

Usage Boundaries

The Who&When dataset is used for failure-attribution evaluation.

It is not the production domain model and should not determine the internal architecture of the application.

The production pipeline should continue to operate on canonical traces:

Who&When record
      ↓
WhoWhenTraceAdapter
      ↓
CanonicalTrace
      ↓
Context Management
      ↓
Failure Attribution

Benchmark-specific labels are introduced only when evaluating predictions.

Notes for Contributors and AI Agents

When working with this dataset:

Preserve original history ordering.

Preserve empty messages.

Do not infer missing tool calls, timestamps, status, or agent identity.

Do not leak benchmark labels into attribution inputs.

Keep Parquet loading outside the production adapter.

Keep persistence outside the adapter.

Treat malformed trace structure separately from invalid benchmark annotations.

Do not silently repair inconsistent benchmark labels.

Keep production trace contracts framework-independent.

Use the small committed fixtures for tests and the full local dataset for experiments.

Citation and License

This repository does not redefine the dataset's licence or academic citation requirements.

Before publishing results, redistributing the dataset, or submitting the final thesis, use the citation and licence information provided by the original Who&When dataset/repository.