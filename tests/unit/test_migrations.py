from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]


def migration_config(output: StringIO) -> Config:
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def test_migration_has_single_head_and_renders_postgresql_ddl():
    output = StringIO()
    config = migration_config(output)
    assert ScriptDirectory.from_config(config).get_heads() == ["0006"]
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE runs" in sql
    assert "original_trace BYTEA NOT NULL" in sql
    assert "CREATE TABLE trace_steps" in sql
    assert "uq_trace_steps_run_sequence" in sql
    assert "FOREIGN KEY(run_id) REFERENCES runs (id)" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql


def test_downgrade_renders_in_dependency_order():
    output = StringIO()
    command.downgrade(migration_config(output), "0006:base", sql=True)
    sql = output.getvalue()
    assert sql.index("DROP TABLE trace_steps") < sql.index("DROP TABLE runs")
