"""Database engine, session factory, and schema bootstrap."""

import os
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

# Columns added after the first release, applied to databases created before
# them. Each entry is (table, column, ALTER TABLE type clause). SQLite only
# allows adding nullable columns without a default, which is what these are.
ADDED_COLUMNS = (
    ("tasks", "parent_id", "INTEGER REFERENCES tasks(id) ON DELETE CASCADE"),
    ("tasks", "estimate_minutes", "INTEGER"),
    # Superseded by the task_assignees table below. The column is still created
    # so that a database predating it can be read by the backfill, after which
    # it is left in place and unused: SQLite cannot drop a column without
    # rebuilding the table, and an unused nullable column is harmless.
    ("tasks", "assignee_id", "INTEGER REFERENCES members(id) ON DELETE SET NULL"),
)

DEFAULT_DB = Path(__file__).resolve().parent.parent / "pmtool.db"
DB_PATH = Path(os.environ.get("PMTOOL_DB", DEFAULT_DB))
ENGINE = create_engine(f"sqlite:///{DB_PATH}", future=True)
SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, future=True)


@event.listens_for(ENGINE, "connect")
def _enable_foreign_keys(dbapi_connection, connection_record) -> None:
    """SQLite ignores FK constraints unless enabled per connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def migrate() -> list[str]:
    """Add columns missing from an older database. Returns what it changed.

    create_all only creates whole tables, so a table that already exists keeps
    its old shape. Existing rows get NULL for the new columns, which is what
    the models expect for a task with no parent and no estimate.
    """
    inspector = inspect(ENGINE)
    applied: list[str] = []
    with ENGINE.begin() as connection:
        for table, column, type_clause in ADDED_COLUMNS:
            if not inspector.has_table(table):
                continue
            existing = {col["name"] for col in inspector.get_columns(table)}
            if column in existing:
                continue
            connection.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {column} {type_clause}")
            )
            applied.append(f"{table}.{column}")
    return applied


def backfill_task_assignees() -> int:
    """Copy single assignee_id values into task_assignees. Returns rows moved.

    Tasks used to hold one assignee in a column; they now hold many through a
    join table. Any task still carrying a column value that is not already in
    the join table is copied across, so upgrading keeps every assignment.
    """
    inspector = inspect(ENGINE)
    if not inspector.has_table("tasks") or not inspector.has_table("task_assignees"):
        return 0
    if "assignee_id" not in {col["name"] for col in inspector.get_columns("tasks")}:
        return 0

    with ENGINE.begin() as connection:
        result = connection.execute(
            text(
                """
                INSERT INTO task_assignees (task_id, member_id)
                SELECT t.id, t.assignee_id
                FROM tasks t
                JOIN members m ON m.id = t.assignee_id
                WHERE t.assignee_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM task_assignees ta
                      WHERE ta.task_id = t.id AND ta.member_id = t.assignee_id
                  )
                """
            )
        )
        moved = result.rowcount or 0
        if moved:
            # Clear the old column so the backfill cannot double-apply and so
            # the join table is unambiguously the only source of truth.
            connection.execute(text("UPDATE tasks SET assignee_id = NULL"))
    return moved


def init_db() -> None:
    Base.metadata.create_all(ENGINE)
    migrate()
    backfill_task_assignees()


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
