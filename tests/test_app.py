"""Route tests. The app module is imported against a temp database file."""

import importlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PMTOOL_DB", str(tmp_path / "test.db"))
    for name in ("app.main", "app.db", "app.models", "app.services", "app.cli"):
        sys.modules.pop(name, None)
    main = importlib.import_module("app.main")
    with TestClient(main.app) as c:
        yield c


def make_project(client, name="Website", purpose="Ship the marketing site"):
    resp = client.post(
        "/projects", data={"name": name, "purpose": purpose}, follow_redirects=False
    )
    assert resp.status_code == 303
    return int(resp.headers["location"].rsplit("/", 1)[1])


def make_task(client, project_id, title="Draft copy", **extra):
    payload = {"title": title, "notes": "", "priority": "med", "due": ""}
    payload.update(extra)
    resp = client.post(f"/projects/{project_id}/tasks", data=payload)
    assert resp.status_code == 200, resp.text
    return int(resp.text.split('id="task-', 1)[1].split('"', 1)[0])


def test_index_empty(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "No projects yet." in resp.text


def test_create_project_and_show_purpose(client):
    project_id = make_project(client)
    detail = client.get(f"/projects/{project_id}")
    assert "Ship the marketing site" in detail.text
    assert "Website" in client.get("/").text


def test_project_name_required(client):
    assert client.post("/projects", data={"name": "  "}).status_code == 400


def test_task_lifecycle(client):
    project_id = make_project(client)
    task_id = make_task(client, project_id, priority="high", due="2030-01-01")

    row = client.post(f"/tasks/{task_id}/toggle")
    assert 'class="task status-done"' in row.text

    row = client.post(f"/tasks/{task_id}/toggle")
    assert 'class="task status-todo"' in row.text

    edited = client.post(
        f"/tasks/{task_id}/edit",
        data={
            "title": "Draft copy v2",
            "notes": "with the new brief",
            "status": "doing",
            "priority": "low",
            "due": "",
        },
    )
    assert "Draft copy v2" in edited.text
    assert "with the new brief" in edited.text

    assert client.delete(f"/tasks/{task_id}").status_code == 200
    assert "Draft copy v2" not in client.get(f"/projects/{project_id}").text


def test_bad_due_and_bad_choice_rejected(client):
    project_id = make_project(client)
    bad_due = client.post(
        f"/projects/{project_id}/tasks", data={"title": "x", "due": "01-01-2030"}
    )
    assert bad_due.status_code == 400

    task_id = make_task(client, project_id)
    bad_status = client.post(
        f"/tasks/{task_id}/edit",
        data={"title": "x", "status": "nope", "priority": "med", "due": ""},
    )
    assert bad_status.status_code == 400


def test_progress_and_filter(client):
    project_id = make_project(client)
    done_id = make_task(client, project_id, title="one")
    make_task(client, project_id, title="two")
    client.post(f"/tasks/{done_id}/toggle")

    assert "50% done" in client.get("/").text

    client.post(
        f"/projects/{project_id}/edit",
        data={"name": "Website", "purpose": "p", "status": "archived"},
    )
    assert "Website" not in client.get("/").text
    assert "Website" in client.get("/?status=archived").text


def test_delete_project_removes_tasks(client):
    project_id = make_project(client)
    task_id = make_task(client, project_id)
    client.post(f"/projects/{project_id}/delete")
    assert client.get(f"/projects/{project_id}").status_code == 404
    assert client.post(f"/tasks/{task_id}/toggle").status_code == 404


def test_missing_ids_404(client):
    assert client.get("/projects/999").status_code == 404
    assert client.get("/tasks/999/edit").status_code == 404


# --- tags -------------------------------------------------------------------


def test_tags_normalized_and_rendered(client):
    project_id = make_project(client)
    row = client.post(
        f"/projects/{project_id}/tasks",
        data={"title": "Tagged", "tags": "API, Docs  api"},
    )
    assert "#api" in row.text
    assert "#docs" in row.text
    assert row.text.count("#api") == 1


def test_tag_filter_in_search(client):
    project_id = make_project(client)
    client.post(f"/projects/{project_id}/tasks", data={"title": "Has tag", "tags": "api"})
    client.post(f"/projects/{project_id}/tasks", data={"title": "No tag"})

    hits = client.get("/search?tag=api")
    assert "Has tag" in hits.text
    assert "No tag" not in hits.text


def test_removing_last_tag_prunes_it(client):
    project_id = make_project(client)
    task_id = make_task(client, project_id, tags="temporary")
    assert "temporary" in client.get("/search").text

    client.post(
        f"/tasks/{task_id}/edit",
        data={"title": "Draft copy", "status": "todo", "priority": "med", "tags": ""},
    )
    assert 'value="temporary"' not in client.get("/search").text


# --- search -----------------------------------------------------------------


def test_search_matches_title_notes_and_project(client):
    project_id = make_project(client, name="Apollo", purpose="p")
    make_task(client, project_id, title="Write spec", notes="covering the API")
    make_task(client, project_id, title="Unrelated")

    assert "Write spec" in client.get("/search?q=spec").text
    assert "Write spec" in client.get("/search?q=API").text
    assert "Unrelated" in client.get("/search?q=apollo").text
    assert "No tasks match." in client.get("/search?q=zzzz").text


def test_search_status_and_priority_filters(client):
    project_id = make_project(client)
    high = make_task(client, project_id, title="High one", priority="high")
    make_task(client, project_id, title="Low one", priority="low")
    client.post(f"/tasks/{high}/toggle")

    only_done = client.get("/search?status=done")
    assert "High one" in only_done.text
    assert "Low one" not in only_done.text

    only_low = client.get("/search?priority=low")
    assert "Low one" in only_low.text
    assert "High one" not in only_low.text


def test_search_overdue_filter(client):
    project_id = make_project(client)
    make_task(client, project_id, title="Late thing", due="2000-01-01")
    make_task(client, project_id, title="Future thing", due="2999-01-01")

    overdue = client.get("/search?overdue=1")
    assert "Late thing" in overdue.text
    assert "Future thing" not in overdue.text


def test_search_htmx_returns_fragment(client):
    make_project(client)
    resp = client.get("/search?q=", headers={"HX-Request": "true"})
    assert "<html" not in resp.text
    assert 'id="results"' in resp.text


# --- subtasks ---------------------------------------------------------------


def test_subtask_creation_and_progress(client):
    project_id = make_project(client)
    parent = make_task(client, project_id, title="Parent")

    row = client.post(f"/tasks/{parent}/subtasks", data={"title": "Child one"})
    assert "Child one" in row.text
    assert "subtasks 0/1" in row.text

    client.post(f"/tasks/{parent}/subtasks", data={"title": "Child two"})
    detail = client.get(f"/projects/{project_id}")
    assert "subtasks 0/2" in detail.text


def test_completing_parent_completes_subtasks(client):
    project_id = make_project(client)
    parent = make_task(client, project_id, title="Parent")
    client.post(f"/tasks/{parent}/subtasks", data={"title": "Child"})

    row = client.post(f"/tasks/{parent}/toggle")
    assert "subtasks 1/1" in row.text
    assert 'class="subtask status-done"' in row.text


def test_subtasks_do_not_nest_deeper(client):
    project_id = make_project(client)
    parent = make_task(client, project_id, title="Parent")
    child_row = client.post(f"/tasks/{parent}/subtasks", data={"title": "Child"})
    child_id = int(child_row.text.split('id="subtask-', 1)[1].split('"', 1)[0])

    resp = client.post(f"/tasks/{child_id}/subtasks", data={"title": "Grandchild"})
    assert resp.status_code == 400


def test_deleting_parent_deletes_subtasks(client):
    project_id = make_project(client)
    parent = make_task(client, project_id, title="Parent")
    child_row = client.post(f"/tasks/{parent}/subtasks", data={"title": "Child"})
    child_id = int(child_row.text.split('id="subtask-', 1)[1].split('"', 1)[0])

    client.delete(f"/tasks/{parent}")
    assert client.post(f"/tasks/{child_id}/toggle").status_code == 404


# --- time tracking ----------------------------------------------------------


def test_timer_start_stop_and_log(client):
    project_id = make_project(client)
    task_id = make_task(client, project_id)

    started = client.post(f"/tasks/{task_id}/timer/start")
    assert "Stop" in started.text
    assert "Running:" in client.get("/").text

    stopped = client.post(f"/tasks/{task_id}/timer/stop")
    assert "Start" in stopped.text
    assert "Running:" not in client.get("/").text

    logged = client.post(f"/tasks/{task_id}/timer/log", data={"minutes": "1h30m"})
    assert "1h 30m" in logged.text


def test_only_one_timer_runs_at_a_time(client):
    project_id = make_project(client)
    first = make_task(client, project_id, title="First")
    second = make_task(client, project_id, title="Second")

    client.post(f"/tasks/{first}/timer/start")
    client.post(f"/tasks/{second}/timer/start")

    assert "Start" in client.get(f"/tasks/{first}/row").text
    assert "Stop" in client.get(f"/tasks/{second}/row").text


def test_completing_task_stops_its_timer(client):
    project_id = make_project(client)
    task_id = make_task(client, project_id)
    client.post(f"/tasks/{task_id}/timer/start")
    client.post(f"/tasks/{task_id}/toggle")
    assert "Running:" not in client.get("/").text


def test_bad_log_amount_rejected(client):
    project_id = make_project(client)
    task_id = make_task(client, project_id)
    assert client.post(f"/tasks/{task_id}/timer/log", data={"minutes": "soon"}).status_code == 400


def test_project_rolls_up_subtask_time(client):
    project_id = make_project(client)
    parent = make_task(client, project_id, title="Parent")
    child_row = client.post(f"/tasks/{parent}/subtasks", data={"title": "Child"})
    child_id = int(child_row.text.split('id="subtask-', 1)[1].split('"', 1)[0])

    client.post(f"/tasks/{child_id}/timer/log", data={"minutes": "30"})
    assert "30m" in client.get(f"/projects/{project_id}").text


# --- export -----------------------------------------------------------------


def test_export_json(client):
    project_id = make_project(client, name="Apollo")
    task_id = make_task(client, project_id, title="Spec", tags="api")
    client.post(f"/tasks/{task_id}/subtasks", data={"title": "Sub"})

    payload = json.loads(client.get("/export.json").text)
    project = payload["projects"][0]
    assert project["name"] == "Apollo"
    assert project["tasks"][0]["tags"] == ["api"]
    assert project["tasks"][0]["subtasks"][0]["title"] == "Sub"


def test_export_csv_has_header_and_rows(client):
    project_id = make_project(client, name="Apollo")
    parent = make_task(client, project_id, title="Spec")
    client.post(f"/tasks/{parent}/subtasks", data={"title": "Sub"})

    body = client.get("/export.csv").text
    lines = [line for line in body.splitlines() if line.strip()]
    assert lines[0].startswith("project,project_purpose")
    assert any(line.startswith("Apollo,") and ",Spec," in line for line in lines[1:])
    assert any(",Spec,Sub," in line for line in lines[1:])


def test_export_markdown(client):
    project_id = make_project(client, name="Apollo", purpose="Land it")
    make_task(client, project_id, title="Spec", tags="api")

    body = client.get("/export.md").text
    assert "## Apollo (active)" in body
    assert "**Purpose:** Land it" in body
    assert "- [ ] Spec" in body
    assert "#api" in body


def test_export_scoped_to_project(client):
    first = make_project(client, name="Apollo")
    make_project(client, name="Gemini")
    make_task(client, first, title="Spec")

    body = client.get(f"/export.md?project_id={first}").text
    assert "Apollo" in body
    assert "Gemini" not in body


def test_export_bad_format_and_missing_project(client):
    assert client.get("/export.xml").status_code == 404
    assert client.get("/export.json?project_id=999").status_code == 404
