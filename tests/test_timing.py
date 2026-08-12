"""Cumulative timing: stopping and restarting resumes from the running total.

Entries are backdated directly so elapsed time is deterministic rather than
depending on how long the test itself takes.
"""

import importlib
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PMTOOL_DB", str(tmp_path / "timing.db"))
    for name in ("app.main", "app.db", "app.models", "app.services", "app.cli"):
        sys.modules.pop(name, None)
    main = importlib.import_module("app.main")
    db = importlib.import_module("app.db")
    services = importlib.import_module("app.services")
    models = importlib.import_module("app.models")

    with TestClient(main.app) as client:
        yield client, db, services, models


def backdate(db, models, task_id, minutes):
    """Push the running entry's start back so it reads as `minutes` elapsed."""
    session = db.SessionLocal()
    try:
        entry = (
            session.query(models.TimeEntry)
            .filter(models.TimeEntry.task_id == task_id, models.TimeEntry.ended_at.is_(None))
            .one()
        )
        entry.started_at = models.utcnow() - timedelta(minutes=minutes)
        session.commit()
    finally:
        session.close()


def seed(client):
    resp = client.post("/projects", data={"name": "Apollo"}, follow_redirects=False)
    project_id = int(resp.headers["location"].rsplit("/", 1)[1])
    row = client.post(f"/projects/{project_id}/tasks", data={"title": "Write spec"})
    task_id = int(row.text.split('id="task-', 1)[1].split('"', 1)[0])
    return project_id, task_id


def total_minutes(db, models, task_id):
    session = db.SessionLocal()
    try:
        return session.get(models.Task, task_id).tracked_seconds // 60
    finally:
        session.close()


def test_restart_resumes_from_previous_total(env):
    client, db, services, models = env
    _, task_id = seed(client)

    client.post(f"/tasks/{task_id}/timer/start")
    backdate(db, models, task_id, 10)
    client.post(f"/tasks/{task_id}/timer/stop")
    assert total_minutes(db, models, task_id) == 10

    client.post(f"/tasks/{task_id}/timer/start")
    backdate(db, models, task_id, 3)
    assert total_minutes(db, models, task_id) == 13

    client.post(f"/tasks/{task_id}/timer/stop")
    assert total_minutes(db, models, task_id) == 13


def test_many_sessions_keep_adding_up(env):
    client, db, services, models = env
    _, task_id = seed(client)

    for minutes in (5, 7, 4):
        client.post(f"/tasks/{task_id}/timer/start")
        backdate(db, models, task_id, minutes)
        client.post(f"/tasks/{task_id}/timer/stop")

    assert total_minutes(db, models, task_id) == 16


def test_gap_between_sessions_is_not_counted(env):
    client, db, services, models = env
    _, task_id = seed(client)

    client.post(f"/tasks/{task_id}/timer/start")
    backdate(db, models, task_id, 10)
    client.post(f"/tasks/{task_id}/timer/stop")

    session = db.SessionLocal()
    try:
        entry = session.query(models.TimeEntry).one()
        entry.started_at = models.utcnow() - timedelta(hours=5)
        entry.ended_at = models.utcnow() - timedelta(hours=5) + timedelta(minutes=10)
        session.commit()
    finally:
        session.close()

    assert total_minutes(db, models, task_id) == 10


def test_row_badge_shows_cumulative_while_running(env):
    client, db, services, models = env
    _, task_id = seed(client)

    client.post(f"/tasks/{task_id}/timer/start")
    backdate(db, models, task_id, 10)
    client.post(f"/tasks/{task_id}/timer/stop")

    row = client.post(f"/tasks/{task_id}/timer/start").text
    backdate(db, models, task_id, 3)

    row = client.get(f"/tasks/{task_id}/row").text
    assert "13m" in row
    assert "ticking" in row


def test_banner_shows_cumulative_not_just_session(env):
    client, db, services, models = env
    _, task_id = seed(client)

    client.post(f"/tasks/{task_id}/timer/start")
    backdate(db, models, task_id, 10)
    client.post(f"/tasks/{task_id}/timer/stop")

    client.post(f"/tasks/{task_id}/timer/start")
    backdate(db, models, task_id, 3)

    banner = client.get("/").text
    assert "13m total" in banner
    assert "this session 3m" in banner


def test_logged_time_adds_to_timed_time(env):
    client, db, services, models = env
    _, task_id = seed(client)

    client.post(f"/tasks/{task_id}/timer/start")
    backdate(db, models, task_id, 10)
    client.post(f"/tasks/{task_id}/timer/stop")

    client.post(f"/tasks/{task_id}/timer/log", data={"minutes": "20"})
    assert total_minutes(db, models, task_id) == 30

    client.post(f"/tasks/{task_id}/timer/start")
    backdate(db, models, task_id, 5)
    assert total_minutes(db, models, task_id) == 35


def test_restarting_after_switching_tasks_keeps_both_totals(env):
    client, db, services, models = env
    project_id, first = seed(client)
    row = client.post(f"/projects/{project_id}/tasks", data={"title": "Second"})
    second = int(row.text.split('id="task-', 1)[1].split('"', 1)[0])

    client.post(f"/tasks/{first}/timer/start")
    backdate(db, models, first, 10)

    # Starting the second task stops the first, banking its 10 minutes.
    client.post(f"/tasks/{second}/timer/start")
    backdate(db, models, second, 4)
    assert total_minutes(db, models, first) == 10

    client.post(f"/tasks/{second}/timer/stop")
    client.post(f"/tasks/{first}/timer/start")
    backdate(db, models, first, 6)

    assert total_minutes(db, models, first) == 16
    assert total_minutes(db, models, second) == 4


def test_completing_and_reopening_preserves_total(env):
    client, db, services, models = env
    _, task_id = seed(client)

    client.post(f"/tasks/{task_id}/timer/start")
    backdate(db, models, task_id, 10)
    client.post(f"/tasks/{task_id}/toggle")
    assert total_minutes(db, models, task_id) == 10

    client.post(f"/tasks/{task_id}/toggle")
    client.post(f"/tasks/{task_id}/timer/start")
    backdate(db, models, task_id, 5)
    assert total_minutes(db, models, task_id) == 15


def test_subtask_sessions_roll_up_to_parent(env):
    client, db, services, models = env
    _, parent = seed(client)
    sub_row = client.post(f"/tasks/{parent}/subtasks", data={"title": "Child"})
    child = int(sub_row.text.split('id="subtask-', 1)[1].split('"', 1)[0])

    client.post(f"/tasks/{child}/timer/start")
    backdate(db, models, child, 8)
    client.post(f"/tasks/{child}/timer/stop")
    client.post(f"/tasks/{child}/timer/start")
    backdate(db, models, child, 7)
    client.post(f"/tasks/{child}/timer/stop")

    assert total_minutes(db, models, child) == 15
    assert total_minutes(db, models, parent) == 15


def test_start_twice_does_not_create_a_second_entry(env):
    client, db, services, models = env
    _, task_id = seed(client)

    client.post(f"/tasks/{task_id}/timer/start")
    backdate(db, models, task_id, 10)
    client.post(f"/tasks/{task_id}/timer/start")

    session = db.SessionLocal()
    try:
        assert session.query(models.TimeEntry).count() == 1
    finally:
        session.close()
    assert total_minutes(db, models, task_id) == 10


def test_cli_reports_cumulative(env, capsys):
    client, db, services, models = env
    _, task_id = seed(client)
    cli = importlib.import_module("app.cli")

    client.post(f"/tasks/{task_id}/timer/start")
    backdate(db, models, task_id, 10)
    client.post(f"/tasks/{task_id}/timer/stop")

    cli.main(["start", str(task_id)])
    assert "resuming from 10m" in capsys.readouterr().out

    backdate(db, models, task_id, 3)
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "13m total" in out
    assert "this session 3m" in out

    cli.main(["stop"])
    assert "13m total" in capsys.readouterr().out
