# Worker boundary

`traceguard.process_run(run_id=...)` delegates to `NormalizationService`. The task
owns Celery retry metadata and process-local engine lifecycle. Services own claims,
adaptation, persistence, and failure recording. PostgreSQL is authoritative;
Celery results are disabled. Task messages carry the Run ID and request-ID header.

Late acknowledgement and task-specific worker-loss requeue allow crash recovery.
An expiring PostgreSQL token fences old workers. Four persisted attempts bound
repeated hard-crash loops. Temporary database errors retry at 2/4/8 seconds (three
retries); busy claims retry after lease expiry, up to six retries. Permanent input
failures do not retry. Default soft/hard limits are 60/70 seconds; lease is 90 seconds.

No engine exists before task execution, so prefork workers never inherit a live
connection pool. All derived steps and the normalized state commit together. The
raw submission was committed separately before any task was published.

See the root README for unresolved handoff/retry recovery and operational limits.

`traceguard.attribute_run(attribution_id=...)` is explicitly submitted through the
attribution API; normalization does not publish it. The task delegates to
AttributionService with ContextManager and OpenAILLMClient. It owns only Celery
retry metadata and its process-local engine. PostgreSQL ownership and four durable
claims fence duplicate and stale delivery, while result/evidence commit atomically.
Default provider timeout is 60s, soft/hard limits 90/100s, and claim lease 120s.
SDK retries are disabled; the same 2/4/8s transient and six busy-retry bounds apply.
Trace content, full prompts, credentials, and private reasoning are never logged.
