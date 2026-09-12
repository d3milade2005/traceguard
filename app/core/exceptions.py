from uuid import UUID


class ApplicationError(Exception):
    """An application failure with a fixed, safe public description."""

    code = "APPLICATION_ERROR"
    public_message = "The operation could not be completed."


class DependencyUnavailableError(ApplicationError):
    code = "DEPENDENCY_UNAVAILABLE"
    public_message = "A required service is unavailable."


class RunNotFoundError(ApplicationError):
    code = "RUN_NOT_FOUND"
    public_message = "The requested diagnostic run does not exist."


class InvalidTraceError(ApplicationError):
    code = "INVALID_TRACE"
    public_message = "The trace exceeds the configured size limit."


class TraceAdapterError(ApplicationError):
    code = "TRACE_ADAPTER_ERROR"
    public_message = "The trace structure is invalid or unsupported."


class UnsupportedTraceSourceError(ApplicationError):
    code = "UNSUPPORTED_TRACE_SOURCE"
    public_message = "The trace source is not supported."


class QueuePublishError(ApplicationError):
    code = "QUEUE_PUBLICATION_FAILED"
    public_message = "The run was preserved but could not be queued."

    def __init__(self, *, run_id: UUID | None = None) -> None:
        super().__init__(self.public_message)
        self.run_id = run_id


class ProcessingRetryError(ApplicationError):
    code = "PROCESSING_UNAVAILABLE"
    public_message = "Processing is temporarily unavailable."


class RunClaimedError(ApplicationError):
    """Another worker owns an unexpired claim; delay duplicate delivery."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("Run is already being processed")
        self.retry_after = retry_after
