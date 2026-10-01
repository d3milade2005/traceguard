from typing import Protocol
from uuid import UUID

from app.core.context import ContextPolicy
from app.core.exceptions import ContextBudgetError
from app.schemas.context import (
    AnalysisContext,
    ContextStatistics,
    SequenceRange,
    TruncatedTraceStep,
)
from app.schemas.traces import CanonicalTrace, TraceStep

SelectedStep = TraceStep | TruncatedTraceStep
MAX_SIZE_PASSES = 16


class SizeEstimator(Protocol):
    @property
    def unit(self) -> str: ...

    def estimate(self, serialized: str) -> int:
        """Return a deterministic, monotonic size including serialization overhead."""
        ...


class UTF8SizeEstimator:
    """Count serialized UTF-8 bytes, without claiming model-specific token accuracy."""

    unit = "utf8_bytes"

    def estimate(self, serialized: str) -> int:
        return len(serialized.encode("utf-8"))


class ContextManager:
    """Select structural evidence without persistence, framework, or provider dependencies."""

    def __init__(self, policy: ContextPolicy, estimator: SizeEstimator | None = None) -> None:
        self.policy = policy
        self.estimator = estimator if estimator is not None else UTF8SizeEstimator()

    def build(self, trace: CanonicalTrace, *, run_id: UUID | None = None) -> AnalysisContext:
        original = self._context(trace, trace.steps, run_id, original_size=None)
        if original.statistics.estimated_selected_size <= self.policy.effective_budget:
            return original.model_copy(deep=True)

        original_size = original.statistics.estimated_original_size
        selected = self._anchors(trace, run_id, original_size)
        blocked_head = blocked_tail = False
        # Alternate adjacent growth so neither endpoint window monopolizes the budget.
        for offset in range(
            1, min(len(trace.steps), max(self.policy.head_steps, self.policy.tail_steps))
        ):
            for index, is_head, active in (
                (offset, True, offset < self.policy.head_steps and not blocked_head),
                (
                    len(trace.steps) - 1 - offset,
                    False,
                    offset < self.policy.tail_steps and not blocked_tail,
                ),
            ):
                if not active:
                    continue
                candidate = dict(selected)
                candidate.setdefault(index, trace.steps[index])
                if self._fits(trace, candidate, run_id, original_size):
                    selected = candidate
                elif is_head:
                    blocked_head = True
                else:
                    blocked_tail = True

        # Recent agent handoffs first; include both speakers and bounded neighbours.
        for index in range(len(trace.steps) - 1, 0, -1):
            if trace.steps[index].agent_id == trace.steps[index - 1].agent_id:
                continue
            start = max(0, index - 1 - self.policy.neighbour_steps)
            end = min(len(trace.steps), index + 1 + self.policy.neighbour_steps)
            candidate = dict(selected)
            for position in range(start, end):
                candidate.setdefault(position, trace.steps[position])
            if self._fits(trace, candidate, run_id, original_size):
                selected = candidate

        result = self._context(
            trace, tuple(selected[i] for i in sorted(selected)), run_id, original_size
        )
        if result.statistics.estimated_selected_size > self.policy.effective_budget:
            raise ContextBudgetError
        # Frozen Pydantic models can still contain mutable JSON; never share it with callers.
        return result.model_copy(deep=True)

    def _anchors(
        self, trace: CanonicalTrace, run_id: UUID | None, original_size: int
    ) -> dict[int, SelectedStep]:
        selected: dict[int, SelectedStep] = {
            i: trace.steps[i] for i in sorted({0, len(trace.steps) - 1})
        }
        if self._fits(trace, selected, run_id, original_size):
            return selected

        # First establish a minimal representation of BOTH endpoints, then share space.
        for i, step in list(selected.items()):
            excerpt = self._excerpt(trace.steps[i], keep=0)
            if self.estimator.estimate(excerpt.model_dump_json()) < self.estimator.estimate(
                step.model_dump_json()
            ):
                selected[i] = excerpt
        if not self._fits(trace, selected, run_id, original_size):
            # The task/identities/envelope are never silently shortened or discarded.
            raise ContextBudgetError

        pending = [i for i, step in selected.items() if isinstance(step, TruncatedTraceStep)]
        for position, i in enumerate(pending):
            current = self._context(trace, tuple(selected.values()), run_id, original_size)
            size = current.statistics.estimated_selected_size
            limit = size + (self.policy.effective_budget - size) // (len(pending) - position)
            candidate = dict(selected)
            candidate[i] = trace.steps[i]
            if self._fits(trace, candidate, run_id, original_size, limit):
                selected = candidate
                continue
            low, high = 0, len(trace.steps[i].model_dump_json()) - 1
            while low < high:
                keep = (low + high + 1) // 2
                candidate[i] = self._excerpt(trace.steps[i], keep)
                if self._fits(trace, candidate, run_id, original_size, limit):
                    low = keep
                else:
                    high = keep - 1
            selected[i] = self._excerpt(trace.steps[i], low)
        return selected

    def _excerpt(self, step: TraceStep, keep: int) -> TruncatedTraceStep:
        serialized = step.model_dump_json()
        prefix_length = (keep + 1) // 2
        suffix_length = keep // 2
        excerpt = TruncatedTraceStep(
            sequence=step.sequence,
            agent_id=step.agent_id,
            event_type=step.event_type,
            prefix=serialized[:prefix_length],
            suffix=serialized[-suffix_length:] if suffix_length else "",
            estimated_original_size=self.estimator.estimate(serialized),
            estimated_selected_size=1,
        )
        for _ in range(MAX_SIZE_PASSES):
            size = self.estimator.estimate(excerpt.model_dump_json())
            if size == excerpt.estimated_selected_size:
                return excerpt
            excerpt = excerpt.model_copy(update={"estimated_selected_size": size})
        raise ValueError("Size estimator counters did not converge")

    def _fits(
        self,
        trace: CanonicalTrace,
        selected: dict[int, SelectedStep],
        run_id: UUID | None,
        original_size: int,
        limit: int | None = None,
    ) -> bool:
        context = self._context(
            trace, tuple(selected[i] for i in sorted(selected)), run_id, original_size
        )
        return context.statistics.estimated_selected_size <= (
            self.policy.effective_budget if limit is None else limit
        )

    def _context(
        self,
        trace: CanonicalTrace,
        steps: tuple[SelectedStep, ...],
        run_id: UUID | None,
        original_size: int | None,
    ) -> AnalysisContext:
        omitted: list[SequenceRange] = []
        previous = -1
        for sequence in [*(step.sequence for step in steps), len(trace.steps)]:
            if sequence > previous + 1:
                omitted.append(SequenceRange(start=previous + 1, end=sequence - 1))
            previous = sequence
        full = not omitted and all(isinstance(step, TraceStep) for step in steps)
        context = AnalysisContext(
            run_id=run_id,
            source=trace.source,
            task=trace.task,
            steps=steps,
            full_trace_used=full,
            omitted_ranges=tuple(omitted),
            statistics=ContextStatistics(
                size_unit=self.estimator.unit,
                budget=self.policy.budget,
                reserved_headroom=self.policy.reserved_headroom,
                effective_budget=self.policy.effective_budget,
                original_step_count=len(trace.steps),
                selected_step_count=len(steps),
                estimated_original_size=original_size if original_size is not None else 1,
                estimated_selected_size=1,
                pruning_occurred=not full,
            ),
        )
        # Count the entire JSON envelope, including statistics and omission/excerpt metadata.
        # Updating decimal size counters converges once their digit lengths stop changing.
        for _ in range(MAX_SIZE_PASSES):
            size = self.estimator.estimate(context.model_dump_json())
            if size == context.statistics.estimated_selected_size:
                return context
            stats = context.statistics.model_copy(
                update={
                    "estimated_selected_size": size,
                    "estimated_original_size": size if original_size is None else original_size,
                }
            )
            context = context.model_copy(update={"statistics": stats})
        raise ValueError("Size estimator counters did not converge")
