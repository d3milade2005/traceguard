from enum import StrEnum


class RunStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    NORMALIZED = "normalized"
    FAILED = "failed"
