"""CLI tests. Each test gets a fresh database via the PMTOOL_DB env var."""

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PMTOOL_DB", str(tmp_path / "cli.db"))
    for name in ("app.main", "app.db", "app.models", "app.services", "app.cli"):
        sys.modules.pop(name, None)
    cli = importlib.import_module("app.cli")

    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


def test_add_and_list_projects(run):
    code, out, _ = run("add-project", "Apollo", "--purpose", "Land it")
    assert code == 0
    assert "Created project 1" in out

    _, out, _ = run("projects")
    assert "Apollo" in out
    assert "purpose: Land it" in out


def test_show_project_with_tasks_and_subtasks(run):
    run("add-project", "Apollo")
    run("add-task", "1", "Write spec", "--tags", "api docs", "--priority", "high")
    run("add-task", "1", "Sub work", "--parent", "1")

    _, out, _ = run("show", "1")
    assert "Write spec" in out
    assert "#api" in out
    assert "Sub work" in out


def test_done_and_reopen(run):
    run("add-project", "Apollo")
    run("add-task", "1", "Write spec")

    _, out, _ = run("done", "1")
    assert "is done" in out

    _, out, _ = run("reopen", "1")
    assert "is todo" in out


def test_search_filters(run):
    run("add-project", "Apollo")
    run("add-task", "1", "Write spec", "--tags", "api")
    run("add-task", "1", "Other thing")

    _, out, _ = run("search", "spec")
    assert "Write spec" in out
    assert "Other thing" not in out

    _, out, _ = run("search", "--tag", "api")
    assert "Write spec" in out

    _, out, _ = run("search", "zzzz")
    assert "No tasks match." in out


def test_timer_start_status_stop_and_log(run):
    run("add-project", "Apollo")
    run("add-task", "1", "Write spec")

    _, out, _ = run("start", "1")
    assert "Timer started" in out

    _, out, _ = run("status")
    assert "on task 1" in out

    _, out, _ = run("stop")
    assert "Stopped task 1" in out

    _, out, _ = run("status")
    assert "No timer running." in out

    _, out, _ = run("log", "1", "1h30m")
    assert "Logged 1h 30m" in out


def test_export_json_and_md_to_file(run, tmp_path):
    run("add-project", "Apollo", "--purpose", "Land it")
    run("add-task", "1", "Write spec", "--tags", "api")

    _, out, _ = run("export", "json")
    payload = json.loads(out)
    assert payload["projects"][0]["name"] == "Apollo"

    target = tmp_path / "out.md"
    _, out, _ = run("export", "md", "--out", str(target))
    assert "Wrote" in out
    assert "- [ ] Write spec" in target.read_text()


def test_export_csv_to_stdout(run):
    run("add-project", "Apollo")
    run("add-task", "1", "Write spec")
    _, out, _ = run("export", "csv")
    assert out.splitlines()[0].startswith("project,project_purpose")
    assert "Write spec" in out


def test_errors_exit_nonzero(run):
    code, _, err = run("show", "999")
    assert code == 1
    assert "project not found" in err

    run("add-project", "Apollo")
    code, _, err = run("add-task", "1", "Bad due", "--due", "01-01-2030")
    assert code == 1
    assert "due must be YYYY-MM-DD" in err


def test_rm_task(run):
    run("add-project", "Apollo")
    run("add-task", "1", "Write spec")
    _, out, _ = run("rm-task", "1")
    assert "Deleted task 1" in out

    _, out, _ = run("show", "1")
    assert "No tasks." in out
