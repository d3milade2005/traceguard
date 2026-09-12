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
