"""Schema migration tests against a database built with the pre-subtask schema."""

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OLD_SCHEMA = """
CREATE TABLE projects (
    id INTEGER NOT NULL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    purpose TEXT,
    status VARCHAR(20),
    created_at DATETIME
);
CREATE TABLE tasks (
    id INTEGER NOT NULL PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title VARCHAR(300) NOT NULL,
    notes TEXT,
    status VARCHAR(20),
    priority VARCHAR(10),
    due DATE,
    created_at DATETIME
);
INSERT INTO projects (id, name, purpose, status, created_at)
VALUES (1, 'Legacy', 'kept from before', 'active', '2026-01-01 00:00:00');
INSERT INTO tasks (id, project_id, title, notes, status, priority, due, created_at)
VALUES (1, 1, 'Old task', '', 'todo', 'med', NULL, '2026-01-01 00:00:00');
"""


@pytest.fixture()
def legacy_db(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(OLD_SCHEMA)
    connection.commit()
    connection.close()

    monkeypatch.setenv("PMTOOL_DB", str(path))
    for name in ("app.main", "app.db", "app.models", "app.services", "app.cli"):
        sys.modules.pop(name, None)
    return path


def columns(path, table="tasks"):
    connection = sqlite3.connect(path)
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


def test_migration_adds_missing_columns(legacy_db):
    assert "parent_id" not in columns(legacy_db)

    db = importlib.import_module("app.db")
    db.init_db()

    present = columns(legacy_db)
    assert "parent_id" in present
    assert "estimate_minutes" in present
    assert "assignee_id" in present


def test_migration_is_idempotent(legacy_db):
    db = importlib.import_module("app.db")
    db.init_db()
    assert db.migrate() == []


def test_migration_keeps_existing_rows(legacy_db):
    db = importlib.import_module("app.db")
    db.init_db()

    from app.models import Project

    session = db.SessionLocal()
    try:
        project = session.get(Project, 1)
        assert project.name == "Legacy"
        assert project.purpose == "kept from before"
        assert [t.title for t in project.tasks] == ["Old task"]
        assert project.tasks[0].parent_id is None
        assert project.tasks[0].estimate_minutes is None
        assert project.tasks[0].assignees == []
        assert project.members == []
    finally:
        session.close()


def test_legacy_project_page_renders(legacy_db):
    """The reported crash: opening a project from a pre-migration database."""
    from fastapi.testclient import TestClient

    main = importlib.import_module("app.main")
    with TestClient(main.app) as client:
        assert client.get("/").status_code == 200
        detail = client.get("/projects/1")
        assert detail.status_code == 200
        assert "Old task" in detail.text


def test_new_features_work_on_migrated_db(legacy_db):
    from fastapi.testclient import TestClient

    main = importlib.import_module("app.main")
    with TestClient(main.app) as client:
        row = client.post("/tasks/1/subtasks", data={"title": "New subtask"})
        assert row.status_code == 200
        assert "New subtask" in row.text

        edited = client.post(
            "/tasks/1/edit",
            data={
                "title": "Old task",
                "status": "todo",
                "priority": "med",
                "tags": "migrated",
                "estimate": "1h",
            },
        )
        assert "#migrated" in edited.text
        assert "est 1h" in edited.text

        team = client.post("/projects/1/members", data={"name": "Ada"})
        assert "Ada" in team.text
        assigned = client.post("/tasks/1/assign", data={"assignee": "Ada"})
        assert "@Ada" in assigned.text
