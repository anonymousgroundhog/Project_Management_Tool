"""Team member and assignment tests, over both the web routes and the CLI."""

import importlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PMTOOL_DB", str(tmp_path / "members.db"))
    for name in ("app.main", "app.db", "app.models", "app.services", "app.cli"):
        sys.modules.pop(name, None)
    main = importlib.import_module("app.main")
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PMTOOL_DB", str(tmp_path / "members_cli.db"))
    for name in ("app.main", "app.db", "app.models", "app.services", "app.cli"):
        sys.modules.pop(name, None)
    cli = importlib.import_module("app.cli")

    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


def make_project(client, name="Apollo"):
    resp = client.post(
        "/projects", data={"name": name, "purpose": "p"}, follow_redirects=False
    )
    return int(resp.headers["location"].rsplit("/", 1)[1])


def make_task(client, project_id, title="Write spec", **extra):
    resp = client.post(
        f"/projects/{project_id}/tasks", data=dict({"title": title}, **extra)
    )
    assert resp.status_code == 200, resp.text
    return int(resp.text.split('id="task-', 1)[1].split('"', 1)[0])


def member_id(client, name):
    page = client.get("/members").text
    marker = f'value="{name}"'
    head = page[: page.index(marker)]
    return int(head.rsplit("/members/", 1)[1].split("/", 1)[0])


# --- member records ---------------------------------------------------------


def test_create_and_list_members(client):
    client.post("/members", data={"name": "Ada", "email": "ada@x.io", "role": "lead"})
    page = client.get("/members").text
    assert "Ada" in page
    assert "ada@x.io" in page
    assert "lead" in page


def test_member_names_are_unique(client):
    client.post("/members", data={"name": "Ada"})
    dupe = client.post("/members", data={"name": "ada"})
    assert dupe.status_code == 400
    assert "already exists" in dupe.text


def test_member_name_required(client):
    assert client.post("/members", data={"name": "   "}).status_code == 400


def test_edit_member(client):
    client.post("/members", data={"name": "Ada"})
    mid = member_id(client, "Ada")
    client.post(f"/members/{mid}/edit", data={"name": "Ada L", "role": "architect"})
    page = client.get("/members").text
    assert "Ada L" in page
    assert "architect" in page


# --- project membership -----------------------------------------------------


def test_add_member_to_project_creates_new_person(client):
    project_id = make_project(client)
    team = client.post(f"/projects/{project_id}/members", data={"name": "Grace"})
    assert "Grace" in team.text
    assert "Grace" in client.get("/members").text


def test_add_existing_member_does_not_duplicate(client):
    project_id = make_project(client)
    client.post("/members", data={"name": "Ada"})
    client.post(f"/projects/{project_id}/members", data={"name": "Ada"})
    team = client.post(f"/projects/{project_id}/members", data={"name": "Ada"})
    assert team.text.count('class="member-chip"') == 1
    assert client.get("/members").text.count('value="Ada"') == 1


def test_project_page_lists_team(client):
    project_id = make_project(client)
    client.post(f"/projects/{project_id}/members", data={"name": "Ada"})
    assert "Ada" in client.get(f"/projects/{project_id}").text


# --- assignment -------------------------------------------------------------


def test_assign_task_to_member(client):
    project_id = make_project(client)
    client.post(f"/projects/{project_id}/members", data={"name": "Ada"})
    mid = member_id(client, "Ada")
    task_id = make_task(client, project_id)

    row = client.post(f"/tasks/{task_id}/assign", data={"assignee": str(mid)})
    assert "@Ada" in row.text
    assert "unassigned" not in row.text.split("task-actions")[0]


def test_assign_at_creation_and_by_name(client):
    project_id = make_project(client)
    client.post(f"/projects/{project_id}/members", data={"name": "Ada"})
    row = client.post(
        f"/projects/{project_id}/tasks", data={"title": "Spec", "assignee": "Ada"}
    )
    assert "@Ada" in row.text


def test_cannot_assign_non_member(client):
    project_id = make_project(client)
    client.post("/members", data={"name": "Outsider"})
    task_id = make_task(client, project_id)

    resp = client.post(f"/tasks/{task_id}/assign", data={"assignee": "Outsider"})
    assert resp.status_code == 400
    assert "not a member" in resp.text


def test_cannot_assign_unknown_person(client):
    project_id = make_project(client)
    task_id = make_task(client, project_id)
    resp = client.post(f"/tasks/{task_id}/assign", data={"assignee": "Nobody"})
    assert resp.status_code == 400
    assert "no member named" in resp.text


def test_unassign_clears_assignee(client):
    project_id = make_project(client)
    client.post(f"/projects/{project_id}/members", data={"name": "Ada"})
    task_id = make_task(client, project_id, assignee="Ada")

    row = client.post(f"/tasks/{task_id}/assign", data={"assignee": ""})
    assert "@Ada" not in row.text
    assert "unassigned" in row.text


def test_assign_subtask(client):
    project_id = make_project(client)
    client.post(f"/projects/{project_id}/members", data={"name": "Ada"})
    parent = make_task(client, project_id, title="Parent")
    sub_row = client.post(
        f"/tasks/{parent}/subtasks", data={"title": "Child", "assignee": "Ada"}
    )
    assert "@Ada" in sub_row.text

    sub_id = int(sub_row.text.split('id="subtask-', 1)[1].split('"', 1)[0])
    cleared = client.post(f"/tasks/{sub_id}/assign", data={"assignee": ""})
    assert cleared.status_code == 200


def test_edit_task_keeps_and_changes_assignee(client):
    project_id = make_project(client)
    client.post(f"/projects/{project_id}/members", data={"name": "Ada"})
    client.post(f"/projects/{project_id}/members", data={"name": "Grace"})
    task_id = make_task(client, project_id, assignee="Ada")

    row = client.post(
        f"/tasks/{task_id}/edit",
        data={
            "title": "Write spec",
            "status": "todo",
            "priority": "med",
            "assignee": "Grace",
        },
    )
    assert "@Grace" in row.text
    assert "@Ada" not in row.text


# --- removal behaviour ------------------------------------------------------


def test_removing_member_from_project_clears_their_assignments(client):
    project_id = make_project(client)
    client.post(f"/projects/{project_id}/members", data={"name": "Ada"})
    mid = member_id(client, "Ada")
    task_id = make_task(client, project_id, assignee="Ada")

    client.post(f"/projects/{project_id}/members/{mid}/remove")
    row = client.get(f"/tasks/{task_id}/row").text
    assert "@Ada" not in row
    assert "unassigned" in row


def test_deleting_member_keeps_tasks_but_unassigns(client):
    project_id = make_project(client)
    client.post(f"/projects/{project_id}/members", data={"name": "Ada"})
    mid = member_id(client, "Ada")
    task_id = make_task(client, project_id, assignee="Ada")

    client.post(f"/members/{mid}/delete")
    detail = client.get(f"/projects/{project_id}")
    assert "Write spec" in detail.text
    assert "@Ada" not in detail.text
    assert client.get(f"/tasks/{task_id}/row").status_code == 200


def test_deleting_project_leaves_member(client):
    project_id = make_project(client)
    client.post(f"/projects/{project_id}/members", data={"name": "Ada"})
    client.post(f"/projects/{project_id}/delete")
    assert "Ada" in client.get("/members").text


def test_member_on_two_projects_is_one_record(client):
    first = make_project(client, "Apollo")
    second = make_project(client, "Gemini")
    client.post(f"/projects/{first}/members", data={"name": "Ada"})
    client.post(f"/projects/{second}/members", data={"name": "Ada"})

    assert client.get("/members").text.count('value="Ada"') == 1
    assert "Apollo" in client.get("/members").text
    assert "Gemini" in client.get("/members").text


def test_missing_member_404(client):
    assert client.post("/members/999/delete").status_code == 404


# --- search -----------------------------------------------------------------


def test_search_by_assignee_and_unassigned(client):
    project_id = make_project(client)
    client.post(f"/projects/{project_id}/members", data={"name": "Ada"})
    make_task(client, project_id, title="Hers", assignee="Ada")
    make_task(client, project_id, title="Nobodys")

    mine = client.get("/search?assignee=Ada")
    assert "Hers" in mine.text
    assert "Nobodys" not in mine.text

    free = client.get("/search?assignee=unassigned")
    assert "Nobodys" in free.text
    assert "Hers" not in free.text


def test_search_unknown_assignee_is_rejected(client):
    make_project(client)
    assert client.get("/search?assignee=Ghost").status_code == 400


# --- export -----------------------------------------------------------------


def test_export_includes_members_and_assignees(client):
    project_id = make_project(client)
    client.post(f"/projects/{project_id}/members", data={"name": "Ada"})
    task_id = make_task(client, project_id, assignee="Ada")
    client.post(f"/tasks/{task_id}/subtasks", data={"title": "Child", "assignee": "Ada"})

    payload = json.loads(client.get("/export.json").text)
    project = payload["projects"][0]
    assert [m["name"] for m in project["members"]] == ["Ada"]
    assert project["tasks"][0]["assignees"] == ["Ada"]
    assert project["tasks"][0]["subtasks"][0]["assignees"] == ["Ada"]

    csv_body = client.get("/export.csv").text
    assert "project_members" in csv_body.splitlines()[0]
    assert "assignees" in csv_body.splitlines()[0]
    assert "Ada" in csv_body

    md_body = client.get("/export.md").text
    assert "**Team:** Ada" in md_body
    assert "@Ada" in md_body


# --- CLI --------------------------------------------------------------------


def test_cli_member_and_assignment_flow(run):
    run("add-project", "Apollo")
    _, out, _ = run("add-member", "Ada", "--role", "lead", "--email", "ada@x.io")
    assert "Created member 1" in out

    _, out, _ = run("members")
    assert "Ada" in out and "lead" in out

    _, out, _ = run("team-add", "1", "Ada")
    assert "Ada added to Apollo" in out

    run("add-task", "1", "Write spec", "--assignee", "Ada")
    _, out, _ = run("show", "1")
    assert "Team: Ada (lead)" in out
    assert "@Ada" in out

    _, out, _ = run("search", "--assignee", "Ada")
    assert "Write spec" in out

    _, out, _ = run("unassign", "1")
    assert "is unassigned" in out

    _, out, _ = run("search", "--assignee", "unassigned")
    assert "Write spec" in out

    _, out, _ = run("assign", "1", "Ada")
    assert "assigned to Ada" in out


def test_cli_team_add_creates_unknown_person(run):
    run("add-project", "Apollo")
    _, out, _ = run("team-add", "1", "Grace")
    assert "Grace added to Apollo" in out
    _, out, _ = run("members")
    assert "Grace" in out


def test_cli_rejects_assigning_non_member(run):
    run("add-project", "Apollo")
    run("add-member", "Outsider")
    run("add-task", "1", "Write spec")
    code, _, err = run("assign", "1", "Outsider")
    assert code == 1
    assert "not a member" in err


def test_cli_team_remove_clears_assignments(run):
    run("add-project", "Apollo")
    run("team-add", "1", "Ada")
    run("add-task", "1", "Write spec", "--assignee", "Ada")

    _, out, _ = run("team-remove", "1", "Ada")
    assert "removed from Apollo" in out

    _, out, _ = run("show", "1")
    assert "@Ada" not in out


def test_cli_rm_member_unassigns(run):
    run("add-project", "Apollo")
    run("team-add", "1", "Ada")
    run("add-task", "1", "Write spec", "--assignee", "Ada")

    _, out, _ = run("rm-member", "1")
    assert "now unassigned" in out

    _, out, _ = run("show", "1")
    assert "Write spec" in out
    assert "@Ada" not in out


def test_cli_duplicate_member_rejected(run):
    run("add-member", "Ada")
    code, _, err = run("add-member", "Ada")
    assert code == 1
    assert "already exists" in err
