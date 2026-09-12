# Worker boundary

The worker responds to Celery control ping but has no application tasks yet.
Celery results are disabled; they are never diagnostic run state.

Add thin task functions here when trace ingestion exists. Each task should call a
service composed with a process-local SQLAlchemy engine/session factory. Do not
share a live connection pool across prefork workers.

Submission must commit the Run and exact original trace before publishing its ID.
Broker publication can fail after commit: preserve the run and explicitly handle
that handoff failure. Never return a successful submission before queue acceptance.

For processing, add durable lifecycle states and an atomic claim/check before
side effects. Persist safe failure codes in a fresh transaction after rollback.
`RunService.record_failure` supports this boundary and repeated failure recording.
A lost worker needs a deliberate recovery policy; late acknowledgement alone does
not provide exactly-once execution.

Messages are JSON, durable, and confirmed. Prefetch is one. Broker reconnection
and publication retries are bounded. Worker-loss auto-requeue is off to avoid
poison-message loops. Add task-specific time limits and bounded retries only for
known transient failures when real tasks arrive. Shutdown grace must exceed task
limits. Arguments should contain IDs, never traces. Pass `run_id` as a keyword
argument for automatic logging correlation.
