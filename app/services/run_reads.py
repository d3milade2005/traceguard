"""Bounded projections over durable stage records; never loads trace or artifact bodies."""

from typing import cast
from uuid import UUID

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import RunNotFoundError
from app.db.models import (
    Analysis,
    Attribution,
    GeneratedTest,
    RegressionSpecOperation,
    RegressionValidation,
    Run,
    TraceStep,
)
from app.schemas.runs import DashboardSummary, RunLedgerItem, RunListQuery, RunPage, RunSummary


def ledger_query() -> Select[tuple[object, ...]]:
    # Analysis IDs are authoritative. Standalone stage requests fall back to the latest
    # durable artifact, using a UUID tie-break so joins never multiply ledger rows.
    generated_id = (
        select(GeneratedTest.id)
        .where(
            GeneratedTest.regression_spec_id == RegressionSpecOperation.id,
        )
        .order_by(GeneratedTest.created_at.desc(), GeneratedTest.id.desc())
        .limit(1)
        .correlate(
            RegressionSpecOperation,
        )
        .scalar_subquery()
    )
    validation_id = (
        select(RegressionValidation.id)
        .where(
            RegressionValidation.generated_test_id == GeneratedTest.id,
        )
        .order_by(RegressionValidation.created_at.desc(), RegressionValidation.id.desc())
        .limit(1)
        .correlate(
            GeneratedTest,
        )
        .scalar_subquery()
    )
    expired = (Analysis.status == "active") & (Analysis.deadline_at <= func.clock_timestamp())
    analysis_status = case((expired, "failed"), else_=Analysis.status)
    stop_reason = case((expired, "ANALYSIS_DEADLINE_EXCEEDED"), else_=Analysis.stop_reason)
    attention = func.coalesce(
        or_(
            Run.status == "failed",
            analysis_status.in_(("failed", "review_required")),
            Attribution.status == "failed",
            Attribution.decision == "abstain",
            RegressionSpecOperation.status == "failed",
            RegressionSpecOperation.disposition == "needs_review",
            GeneratedTest.status == "failed",
            RegressionValidation.status == "failed",
            RegressionValidation.verdict.in_(
                ("not_reproduced", "not_discriminating", "inconclusive")
            ),
        ),
        False,
    )
    statement = (
        select(
            Run.id,
            Run.source,
            Run.status,
            Run.created_at,
            Analysis.id.label("analysis_id"),
            analysis_status.label("analysis_status"),
            Analysis.stage.label("analysis_stage"),
            stop_reason.label("analysis_stop_reason"),
            Attribution.id.label("attribution_id"),
            Attribution.status.label("attribution_status"),
            Attribution.responsible_agent,
            Attribution.decisive_sequence,
            Attribution.confidence,
            RegressionSpecOperation.id.label("regression_spec_id"),
            RegressionSpecOperation.disposition.label("regression_disposition"),
            GeneratedTest.id.label("generated_test_id"),
            RegressionValidation.id.label("validation_id"),
            RegressionValidation.verdict.label("validation_verdict"),
            attention.label("needs_attention"),
        )
        .select_from(Run)
        .outerjoin(Analysis, Analysis.run_id == Run.id)
        .outerjoin(
            Attribution,
            Attribution.run_id == Run.id,
        )
        .outerjoin(
            RegressionSpecOperation, RegressionSpecOperation.attribution_id == Attribution.id
        )
        .outerjoin(
            GeneratedTest,
            GeneratedTest.id == func.coalesce(Analysis.generated_test_id, generated_id),
        )
        .outerjoin(
            RegressionValidation,
            RegressionValidation.id == func.coalesce(Analysis.validation_id, validation_id),
        )
    )
    return cast(Select[tuple[object, ...]], statement)


class RunReadService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def list(self, query: RunListQuery) -> RunPage:
        rows = ledger_query().subquery()
        statement = select(rows)
        for name in (
            "status",
            "analysis_status",
            "responsible_agent",
            "validation_verdict",
            "needs_attention",
        ):
            value = getattr(query, name)
            if value is not None:
                statement = statement.where(rows.c[name] == value)
        with self.sessions() as session:
            total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
            items = (
                session.execute(
                    statement.order_by(
                        rows.c.created_at.desc(),
                        rows.c.id.desc(),
                    )
                    .limit(query.limit)
                    .offset(query.offset)
                )
                .mappings()
                .all()
            )
            return RunPage(
                items=[RunLedgerItem.model_validate(row) for row in items],
                total=total,
                limit=query.limit,
                offset=query.offset,
            )

    def summary(self, run_id: UUID) -> RunSummary:
        count = select(func.count(TraceStep.id)).where(TraceStep.run_id == Run.id).scalar_subquery()
        statement = (
            ledger_query()
            .add_columns(
                Run.updated_at,
                Run.failure_code,
                Run.failed_at,
                count.label("normalized_step_count"),
            )
            .where(Run.id == run_id)
        )
        with self.sessions() as session:
            row = session.execute(statement).mappings().one_or_none()
            if row is None:
                raise RunNotFoundError
            return RunSummary.model_validate(row)

    def dashboard(self) -> DashboardSummary:
        rows = ledger_query().subquery()
        with self.sessions() as session:
            counts = (
                session.execute(
                    select(
                        func.count().label("total_runs"),
                        func.count()
                        .filter(rows.c.analysis_status == "active")
                        .label("active_analyses"),
                        func.count()
                        .filter(rows.c.analysis_status == "review_required")
                        .label("review_required_analyses"),
                        func.count()
                        .filter(rows.c.validation_verdict == "valid")
                        .label("valid_regressions"),
                    ).select_from(rows)
                )
                .mappings()
                .one()
            )
            ordered = select(rows).order_by(rows.c.created_at.desc(), rows.c.id.desc()).limit(5)
            recent = session.execute(ordered).mappings().all()
            attention = (
                session.execute(ordered.where(rows.c.needs_attention.is_(True))).mappings().all()
            )
            return DashboardSummary(
                **counts,
                recent_runs=[RunLedgerItem.model_validate(row) for row in recent],
                needs_attention_runs=[RunLedgerItem.model_validate(row) for row in attention],
            )
