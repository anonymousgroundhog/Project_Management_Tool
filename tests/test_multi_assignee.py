"""Tasks with several assignees, and upgrading from the single-assignee column."""

import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PMTOOL_DB", str(tmp_path / "multi.db"))
    for name in ("app.main", "app.db", "app.models", "app.services", "app.cli"):
        sys.modules.pop(name, None)
    main = importlib.import_module("app.main")
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PMTOOL_DB", str(tmp_path / "multi_cli.db"))
    for name in ("app.main", "app.db", "app.models", "app.services", "app.cli"):
        sys.modules.pop(name, None)
    cli = importlib.import_module("app.cli")

    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


def setup_project(client, *people):
    resp = client.post("/projects", data={"name": "Apollo"}, follow_redirects=False)
    project_id = int(resp.headers["location"].rsplit("/", 1)[1])
    ids = {}
    for person in people:
        client.post(f"/projects/{project_id}/members", data={"name": person})
    page = client.get("/members").text
    for person in people:
        head = page[: page.index(f'value="{person}"')]
        ids[person] = int(head.rsplit("/members/", 1)[1].split("/", 1)[0])
    return project_id, ids


def make_task(client, project_id, title="Write spec", **extra):
    resp = client.post(
        f"/projects/{project_id}/tasks", data=dict({"title": title}, **extra)
    )
    assert resp.status_code == 200, resp.text
    return int(resp.text.split('id="task-', 1)[1].split('"', 1)[0])


# --- assigning several people ----------------------------------------------


def test_assign_two_people_at_creation(client):
    project_id, _ = setup_project(client, "Ada", "Grace")
    row = client.post(
        f"/projects/{project_id}/tasks",
        data={"title": "Pair work", "assignee": ["Ada", "Grace"]},
    )
    assert "@Ada" in row.text
    assert "@Grace" in row.text
    assert "unassigned" not in row.text.split("assign-picker")[0]


def test_assign_replaces_the_whole_set(client):
    project_id, ids = setup_project(client, "Ada", "Grace", "Alan")
    task_id = make_task(client, project_id, assignee=["Ada"])

    row = client.post(
        f"/tasks/{task_id}/assign",
        data={"assignee": [str(ids["Grace"]), str(ids["Alan"])]},
    )
    assert "@Grace" in row.text
    assert "@Alan" in row.text
    assert "@Ada" not in row.text


def test_assign_with_no_selection_clears_everyone(client):
    project_id, _ = setup_project(client, "Ada", "Grace")
    task_id = make_task(client, project_id, assignee=["Ada", "Grace"])

    row = client.post(f"/tasks/{task_id}/assign", data={})
    assert "unassigned" in row.text
    assert "@Ada" not in row.text


def test_add_one_assignee_without_disturbing_others(client):
    project_id, ids = setup_project(client, "Ada", "Grace")
    task_id = make_task(client, project_id, assignee=["Ada"])

    row = client.post(f"/tasks/{task_id}/assign/{ids['Grace']}")
    assert "@Ada" in row.text
    assert "@Grace" in row.text


def test_remove_one_assignee_leaves_the_rest(client):
    project_id, ids = setup_project(client, "Ada", "Grace")
    task_id = make_task(client, project_id, assignee=["Ada", "Grace"])

    row = client.post(f"/tasks/{task_id}/unassign/{ids['Ada']}")
    assert "@Ada" not in row.text
    assert "@Grace" in row.text


def test_duplicate_assignment_is_ignored(client):
    project_id, ids = setup_project(client, "Ada")
    task_id = make_task(client, project_id, assignee=["Ada"])

    row = client.post(f"/tasks/{task_id}/assign/{ids['Ada']}")
    assert row.text.count("@Ada") == 1

    row = client.post(f"/tasks/{task_id}/assign", data={"assignee": ["Ada", "Ada"]})
    assert row.text.count("@Ada") == 1


def test_edit_task_sets_multiple_assignees(client):
    project_id, ids = setup_project(client, "Ada", "Grace")
    task_id = make_task(client, project_id)

    row = client.post(
        f"/tasks/{task_id}/edit",
        data={
            "title": "Write spec",
            "status": "todo",
            "priority": "med",
            "assignee": [str(ids["Ada"]), str(ids["Grace"])],
        },
    )
    assert "@Ada" in row.text
    assert "@Grace" in row.text


def test_subtask_takes_several_assignees(client):
    project_id, _ = setup_project(client, "Ada", "Grace")
    parent = make_task(client, project_id, title="Parent")

    row = client.post(
        f"/tasks/{parent}/subtasks",
        data={"title": "Child", "assignee": ["Ada", "Grace"]},
    )
    assert "@Ada" in row.text
    assert "@Grace" in row.text


def test_non_member_still_rejected_among_valid_ones(client):
    project_id, _ = setup_project(client, "Ada")
    client.post("/members", data={"name": "Outsider"})
    task_id = make_task(client, project_id)

    resp = client.post(f"/tasks/{task_id}/assign", data={"assignee": ["Ada", "Outsider"]})
    assert resp.status_code == 400
    assert "not a member" in resp.text

    # The rejected call must not have applied the valid half either.
    assert "@Ada" not in client.get(f"/tasks/{task_id}/row").text


# --- interaction with removal ----------------------------------------------


def test_removing_one_member_from_project_keeps_the_other_assignee(client):
    project_id, ids = setup_project(client, "Ada", "Grace")
    task_id = make_task(client, project_id, assignee=["Ada", "Grace"])

    client.post(f"/projects/{project_id}/members/{ids['Ada']}/remove")
    row = client.get(f"/tasks/{task_id}/row").text
    assert "@Ada" not in row
    assert "@Grace" in row


def test_deleting_a_member_keeps_the_other_assignee(client):
    project_id, ids = setup_project(client, "Ada", "Grace")
    task_id = make_task(client, project_id, assignee=["Ada", "Grace"])

    client.post(f"/members/{ids['Ada']}/delete")
    row = client.get(f"/tasks/{task_id}/row").text
    assert "@Ada" not in row
    assert "@Grace" in row


def test_unassigned_count_needs_everyone_gone(client):
    project_id, ids = setup_project(client, "Ada", "Grace")
    task_id = make_task(client, project_id, assignee=["Ada", "Grace"])

    client.post(f"/tasks/{task_id}/unassign/{ids['Ada']}")
    assert "1 unassigned" not in client.get(f"/projects/{project_id}").text

    client.post(f"/tasks/{task_id}/unassign/{ids['Grace']}")
    assert "1 unassigned" in client.get(f"/projects/{project_id}").text


# --- search -----------------------------------------------------------------


def test_search_finds_task_by_any_of_its_assignees(client):
    project_id, _ = setup_project(client, "Ada", "Grace")
    make_task(client, project_id, title="Shared", assignee=["Ada", "Grace"])
    make_task(client, project_id, title="Solo", assignee=["Ada"])

    by_ada = client.get("/search?assignee=Ada")
    assert "Shared" in by_ada.text and "Solo" in by_ada.text

    by_grace = client.get("/search?assignee=Grace")
    assert "Shared" in by_grace.text and "Solo" not in by_grace.text


def test_search_unassigned_excludes_multi_assigned(client):
    project_id, _ = setup_project(client, "Ada", "Grace")
    make_task(client, project_id, title="Shared", assignee=["Ada", "Grace"])
    make_task(client, project_id, title="Nobodys")

    free = client.get("/search?assignee=unassigned")
    assert "Nobodys" in free.text
    assert "Shared" not in free.text


# --- export -----------------------------------------------------------------


def test_exports_list_every_assignee(client):
    project_id, _ = setup_project(client, "Ada", "Grace")
    make_task(client, project_id, title="Shared", assignee=["Ada", "Grace"])

    payload = json.loads(client.get("/export.json").text)
    assert payload["projects"][0]["tasks"][0]["assignees"] == ["Ada", "Grace"]

    csv_body = client.get("/export.csv").text
    assert "Ada; Grace" in csv_body

    md_body = client.get("/export.md").text
    assert "@Ada @Grace" in md_body


# --- migration from the single-assignee column ------------------------------


def test_backfill_moves_column_assignment_into_join_table(tmp_path, monkeypatch):
    """A database written before this change keeps its assignments."""
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE projects (
            id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(200) NOT NULL,
            purpose TEXT, status VARCHAR(20), created_at DATETIME
        );
        CREATE TABLE members (
            id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(120) NOT NULL,
            email VARCHAR(200), role VARCHAR(80), created_at DATETIME
        );
        CREATE TABLE project_members (
            project_id INTEGER NOT NULL, member_id INTEGER NOT NULL,
            PRIMARY KEY (project_id, member_id)
        );
        CREATE TABLE tasks (
            id INTEGER NOT NULL PRIMARY KEY, project_id INTEGER NOT NULL,
            parent_id INTEGER, title VARCHAR(300) NOT NULL, notes TEXT,
            status VARCHAR(20), priority VARCHAR(10), due DATE,
            estimate_minutes INTEGER, assignee_id INTEGER, created_at DATETIME
        );
        INSERT INTO projects VALUES (1, 'Apollo', 'p', 'active', '2026-01-01 00:00:00');
        INSERT INTO members VALUES (1, 'Ada', '', 'lead', '2026-01-01 00:00:00');
        INSERT INTO project_members VALUES (1, 1);
        INSERT INTO tasks VALUES
            (1, 1, NULL, 'Old task', '', 'todo', 'med', NULL, NULL, 1, '2026-01-01 00:00:00');
        """
    )
    connection.commit()
    connection.close()

    monkeypatch.setenv("PMTOOL_DB", str(path))
    for name in ("app.main", "app.db", "app.models", "app.services", "app.cli"):
        sys.modules.pop(name, None)
    main = importlib.import_module("app.main")
    db = importlib.import_module("app.db")

    with TestClient(main.app) as client:
        row = client.get("/tasks/1/row").text
        assert "@Ada" in row

        # The old column is cleared so the join table is the only source.
        check = sqlite3.connect(path)
        try:
            assert check.execute("SELECT assignee_id FROM tasks").fetchone()[0] is None
            assert check.execute("SELECT COUNT(*) FROM task_assignees").fetchone()[0] == 1
        finally:
            check.close()

        # Re-running must not duplicate anything.
        assert db.backfill_task_assignees() == 0

        # And a second person can now be added on top of the migrated one.
        client.post("/projects/1/members", data={"name": "Grace"})
        row = client.post("/tasks/1/assign", data={"assignee": ["Ada", "Grace"]}).text
        assert "@Ada" in row and "@Grace" in row


# --- CLI --------------------------------------------------------------------


def test_cli_assign_adds_then_replaces(run):
    run("add-project", "Apollo")
    run("team-add", "1", "Ada")
    run("team-add", "1", "Grace")
    run("team-add", "1", "Alan")
    run("add-task", "1", "Write spec")

    _, out, _ = run("assign", "1", "Ada", "Grace")
    assert "assigned to Ada, Grace" in out

    _, out, _ = run("assign", "1", "Alan")
    assert "assigned to Ada, Alan, Grace" in out

    _, out, _ = run("assign", "1", "Ada", "--replace")
    assert "assigned to Ada" in out
    assert "Grace" not in out


def test_cli_unassign_one_or_all(run):
    run("add-project", "Apollo")
    run("team-add", "1", "Ada")
    run("team-add", "1", "Grace")
    run("add-task", "1", "Write spec", "--assignee", "Ada", "--assignee", "Grace")

    _, out, _ = run("show", "1")
    assert "@Ada" in out and "@Grace" in out

    _, out, _ = run("unassign", "1", "Ada")
    assert "assigned to Grace" in out

    _, out, _ = run("unassign", "1")
    assert "is unassigned" in out


def test_cli_add_task_with_repeated_assignee_flag(run):
    run("add-project", "Apollo")
    run("team-add", "1", "Ada")
    run("team-add", "1", "Grace")
    run("add-task", "1", "Pair work", "--assignee", "Ada", "--assignee", "Grace")

    _, out, _ = run("show", "1")
    assert "@Ada @Grace" in out


def test_cli_search_by_one_of_several(run):
    run("add-project", "Apollo")
    run("team-add", "1", "Ada")
    run("team-add", "1", "Grace")
    run("add-task", "1", "Shared", "--assignee", "Ada", "--assignee", "Grace")

    _, out, _ = run("search", "--assignee", "Grace")
    assert "Shared" in out
