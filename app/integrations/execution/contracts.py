from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.schemas.validations import ExecutionResult, RuntimeIdentity, Variant


@dataclass(frozen=True)
class ExecutionBundle:
    source: str
    test_name: str


class ValidationRunner(Protocol):
    def prepare(self, image_reference: str) -> RuntimeIdentity: ...
    def cleanup_expired(self) -> None: ...
    def cleanup_previous(self, validation_id: UUID, token: UUID) -> None: ...
    def execute(
        self,
        bundle: ExecutionBundle,
        runtime: RuntimeIdentity,
        variant: Variant,
        validation_id: UUID,
        token: UUID,
    ) -> ExecutionResult: ...
