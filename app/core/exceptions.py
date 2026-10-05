from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from app.schemas.attributions import ProviderUsage


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


class RunNotNormalizedError(ApplicationError):
    code = "RUN_NOT_NORMALIZED"
    public_message = "The run does not yet have a complete canonical trace."


class ContextBudgetError(ApplicationError):
    code = "CONTEXT_BUDGET_TOO_SMALL"
    public_message = "The analysis budget cannot hold the task and minimum evidence context."


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


class AttributionNotFoundError(ApplicationError):
    code = "ATTRIBUTION_NOT_FOUND"
    public_message = "The requested attribution does not exist."


class AttributionValidationError(ApplicationError):
    code = "ATTRIBUTION_INVALID"
    public_message = "The attribution does not reference valid visible evidence."


class AttributionInputError(ApplicationError):
    code = "ATTRIBUTION_INPUT_INVALID"
    public_message = "The analysis evidence cannot be safely supplied for attribution."


class AttributionQueuePublishError(QueuePublishError):
    public_message = "The attribution was preserved but could not be queued."

    def __init__(self, *, attribution_id: UUID, run_id: UUID | None = None) -> None:
        super().__init__(run_id=run_id)
        self.attribution_id = attribution_id


class LLMProviderError(ApplicationError):
    code = "LLM_PROVIDER_ERROR"
    public_message = "The attribution provider could not complete the request."


class LLMTransientError(LLMProviderError):
    code = "LLM_TEMPORARILY_UNAVAILABLE"


class LLMConfigurationError(LLMProviderError):
    code = "LLM_CONFIGURATION_INVALID"


class LLMOutputError(LLMProviderError):
    code = "LLM_OUTPUT_INVALID"

    def __init__(self, *, usage: "ProviderUsage | None" = None) -> None:
        super().__init__(self.public_message)
        self.usage = usage


class LLMRefusalError(LLMOutputError):
    code = "LLM_REFUSED"


class LLMIncompleteError(LLMOutputError):
    code = "LLM_RESPONSE_INCOMPLETE"


class LLMOutputLimitError(LLMIncompleteError):
    code = "LLM_OUTPUT_LIMIT"


class RunClaimedError(ApplicationError):
    """Another worker owns an unexpired claim; delay duplicate delivery."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("Run is already being processed")
        self.retry_after = retry_after


class RegressionNotFoundError(ApplicationError):
    code = "REGRESSION_SPEC_NOT_FOUND"
    public_message = "The requested regression specification does not exist."


class AttributionNotCompletedError(ApplicationError):
    code = "ATTRIBUTION_NOT_COMPLETED"
    public_message = "Regression synthesis requires a completed attribution."


class RegressionValidationError(ApplicationError):
    code = "REGRESSION_SPEC_INVALID"
    public_message = "The regression specification is not safely grounded in canonical evidence."


class RegressionInputError(ApplicationError):
    code = "REGRESSION_INPUT_INVALID"
    public_message = "Regression evidence cannot be safely supplied for synthesis."


class RegressionQueuePublishError(QueuePublishError):
    public_message = "The regression specification was preserved but could not be queued."

    def __init__(self, *, regression_spec_id: UUID) -> None:
        super().__init__()
        self.regression_spec_id = regression_spec_id


class CompilationError(ApplicationError):
    code = "COMPILATION_FAILED"
    public_message = "The regression specification could not be compiled."

    def __init__(self, *, generated_test_id: UUID | None = None) -> None:
        super().__init__(self.public_message)
        self.generated_test_id = generated_test_id


class RegressionNotSynthesizableError(CompilationError):
    code = "REGRESSION_NOT_SYNTHESIZABLE"
    public_message = "Compilation requires a completed synthesizable regression specification."


class UnsupportedSpecVersionError(CompilationError):
    code = "REGRESSION_VERSION_UNSUPPORTED"
    public_message = "The regression specification version is not supported by this compiler."


class CompilationInputError(CompilationError):
    code = "COMPILATION_INPUT_INVALID"
    public_message = "The regression specification or scenario snapshot is invalid."


class UnsupportedAssertionError(CompilationInputError):
    code = "COMPILATION_ASSERTION_UNSUPPORTED"
    public_message = "The regression assertion kind is not supported by this compiler."


class InvalidScenarioSnapshotError(CompilationInputError):
    code = "COMPILATION_SCENARIO_INVALID"
    public_message = "The regression scenario snapshot is invalid or does not match canonical data."


class CompilationInvariantError(CompilationError):
    code = "COMPILER_INVARIANT_INVALID"
    public_message = "The compiler could not produce the required artifact structure."


class GeneratedSyntaxError(CompilationError):
    code = "GENERATED_SYNTAX_INVALID"
    public_message = "The compiler output did not pass static syntax validation."


class GeneratedSafetyError(CompilationError):
    code = "GENERATED_SOURCE_UNSAFE"
    public_message = "The compiler output did not pass static safety validation."


class GeneratedTestNotFoundError(ApplicationError):
    code = "GENERATED_TEST_NOT_FOUND"
    public_message = "The requested generated test does not exist."


class CompilationPersistenceError(ApplicationError):
    code = "COMPILATION_PERSISTENCE_UNAVAILABLE"
    public_message = "The generated test could not be safely persisted."


class ValidationArtifactError(ApplicationError):
    code = "VALIDATION_ARTIFACT_INVALID"
    public_message = "The artifact, harness, or linked scenario is not eligible for validation."


class ValidationNotFoundError(ApplicationError):
    code = "VALIDATION_NOT_FOUND"
    public_message = "The requested regression validation does not exist."


class ValidationQueuePublishError(QueuePublishError):
    public_message = "The validation was preserved but could not be queued."

    def __init__(self, *, validation_id: UUID) -> None:
        super().__init__()
        self.validation_id = validation_id


class SandboxUnavailableError(ApplicationError):
    code = "SANDBOX_UNAVAILABLE"
    public_message = "The isolated execution runtime is temporarily unavailable."


class SandboxPolicyError(ApplicationError):
    code = "SANDBOX_POLICY_INVALID"
    public_message = "The isolated execution runtime does not meet the approved policy."


class SandboxCleanupError(SandboxUnavailableError):
    code = "SANDBOX_CLEANUP_FAILED"


class ValidationPersistenceError(ApplicationError):
    code = "VALIDATION_PERSISTENCE_UNAVAILABLE"
    public_message = "Validation state could not be safely persisted."
