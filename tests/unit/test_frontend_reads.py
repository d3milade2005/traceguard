from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.main import create_app
from app.schemas.runs import DashboardSummary, RunPage
from app.services.run_reads import ledger_query


def test_ledger_sql_never_selects_trace_context_spec_or_source_bodies():
    sql = str(ledger_query().compile(dialect=postgresql.dialect()))
    for field in (
        "runs.original_trace",
        "generated_tests.source_code",
        "regression_specs.spec,",
        "attributions.context_manifest",
        "output_diagnostics",
    ):
        assert field not in sql
    assert "LIMIT" in sql and "coalesce(analyses.validation_id" in sql


def test_runs_and_dashboard_routes_are_lightweight_and_filters_validated(settings):
    calls = []

    class Reads:
        def list(self, query):
            calls.append(query)
            return RunPage(items=[], total=0, limit=query.limit, offset=query.offset)

        def dashboard(self):
            return DashboardSummary(
                total_runs=0,
                active_analyses=0,
                review_required_analyses=0,
                valid_regressions=0,
                recent_runs=[],
                needs_attention_runs=[],
            )

    with TestClient(create_app(settings)) as client:
        client.app.state.run_reads = Reads()
        response = client.get(
            "/api/v1/runs?limit=10&offset=20&analysis_status=failed&needs_attention=true"
        )
        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0, "limit": 10, "offset": 20}
        assert calls[0].analysis_status == "failed" and calls[0].needs_attention
        for query in (
            "limit=101",
            "offset=-1",
            "status=bad",
            "validation_verdict=bad",
            "unknown=1",
        ):
            assert client.get("/api/v1/runs?" + query).status_code == 422
        assert client.get("/api/v1/dashboard").json()["total_runs"] == 0
