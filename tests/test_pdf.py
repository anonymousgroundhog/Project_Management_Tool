"""PDF export tests.

Text is read back with pypdf so the assertions check what a PDF reader would
actually show, rather than grepping compressed bytes.
"""

import importlib
import sys
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PMTOOL_DB", str(tmp_path / "pdf.db"))
    for name in ("app.main", "app.db", "app.models", "app.services", "app.cli", "app.pdf"):
        sys.modules.pop(name, None)
    main = importlib.import_module("app.main")
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PMTOOL_DB", str(tmp_path / "pdf_cli.db"))
    for name in ("app.main", "app.db", "app.models", "app.services", "app.cli", "app.pdf"):
        sys.modules.pop(name, None)
    cli = importlib.import_module("app.cli")

    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


def pdf_pages(body: bytes) -> list[str]:
    """Text of each page, via pypdf so the tests read what a reader would."""
    reader = PdfReader(BytesIO(body))
    return [page.extract_text() or "" for page in reader.pages]


def pdf_text(body: bytes) -> str:
    return "\n".join(pdf_pages(body))


def seed(client, with_data=True):
    resp = client.post(
        "/projects", data={"name": "Apollo", "purpose": "Land it"}, follow_redirects=False
    )
    project_id = int(resp.headers["location"].rsplit("/", 1)[1])
    if not with_data:
        return project_id
    client.post(f"/projects/{project_id}/members", data={"name": "Ada"})
    row = client.post(
        f"/projects/{project_id}/tasks",
        data={
            "title": "Write spec",
            "notes": "covering the API",
            "priority": "high",
            "due": "2030-05-01",
            "tags": "api docs",
            "estimate": "1h30m",
            "assignee": ["Ada"],
        },
    )
    task_id = int(row.text.split('id="task-', 1)[1].split('"', 1)[0])
    client.post(f"/tasks/{task_id}/subtasks", data={"title": "Draft outline"})
    client.post(f"/tasks/{task_id}/timer/log", data={"minutes": "45"})
    return project_id


def test_pdf_response_is_a_pdf(client):
    seed(client)
    resp = client.get("/export.pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert 'filename="projects.pdf"' in resp.headers["content-disposition"]
    assert resp.content.startswith(b"%PDF-")
    assert resp.content.rstrip().endswith(b"%%EOF")
    assert len(resp.content) > 1000


def test_pdf_contains_project_and_task_detail(client):
    seed(client)
    text = pdf_text(client.get("/export.pdf").content)

    assert "Apollo" in text
    assert "Land it" in text
    assert "Write spec" in text
    assert "Draft outline" in text
    assert "Ada" in text
    assert "2030-05-01" in text
    assert "api" in text


def test_pdf_shows_progress_and_tracked_time(client):
    seed(client)
    # Durations use non-breaking spaces so they cannot wrap mid-value.
    text = pdf_text(client.get("/export.pdf").content).replace("\xa0", " ")
    assert "45m" in text
    assert "1h 30m" in text
    assert "tracked" in text
    assert "45m / 1h 30m" in text


def test_pdf_scoped_to_one_project(client):
    first = seed(client)
    client.post("/projects", data={"name": "Gemini", "purpose": "second"})

    text = pdf_text(client.get(f"/export.pdf?project_id={first}").content)
    assert "Apollo" in text
    assert "Gemini" not in text


def test_pdf_with_no_projects_still_renders(client):
    resp = client.get("/export.pdf")
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")
    assert "No projects to export." in pdf_text(resp.content)


def test_pdf_with_project_but_no_tasks(client):
    seed(client, with_data=False)
    text = pdf_text(client.get("/export.pdf").content)
    assert "Apollo" in text
    assert "No tasks." in text


def test_pdf_marks_done_tasks(client):
    project_id = seed(client, with_data=False)
    row = client.post(f"/projects/{project_id}/tasks", data={"title": "Finished thing"})
    task_id = int(row.text.split('id="task-', 1)[1].split('"', 1)[0])
    client.post(f"/tasks/{task_id}/toggle")

    text = pdf_text(client.get("/export.pdf").content)
    assert "Finished thing" in text
    assert "done" in text


def test_pdf_lists_several_assignees(client):
    project_id = seed(client, with_data=False)
    client.post(f"/projects/{project_id}/members", data={"name": "Ada"})
    client.post(f"/projects/{project_id}/members", data={"name": "Grace"})
    client.post(
        f"/projects/{project_id}/tasks",
        data={"title": "Pair work", "assignee": ["Ada", "Grace"]},
    )

    text = pdf_text(client.get("/export.pdf").content)
    assert "Ada" in text
    assert "Grace" in text


def test_each_project_starts_a_new_page(client):
    for index in range(4):
        client.post("/projects", data={"name": f"Project {index}", "purpose": "p"})

    pages = pdf_pages(client.get("/export.pdf").content)
    assert len(pages) == 4
    # Newest first, matching the project list order.
    assert "Project 3" in pages[0]
    assert "Project 0" in pages[3]


def test_pdf_uses_only_glyphs_the_standard_fonts_have(client):
    """Characters outside Latin-1 render as a filled box in Helvetica."""
    seed(client)
    text = pdf_text(client.get("/export.pdf").content)
    for char in text:
        if char in "\n\r\t\xa0":
            continue
        char.encode("latin-1")  # raises if the font cannot show it


def test_pdf_checkboxes_are_ascii(client):
    project_id = seed(client, with_data=False)
    row = client.post(f"/projects/{project_id}/tasks", data={"title": "Done thing"})
    done_id = int(row.text.split('id="task-', 1)[1].split('"', 1)[0])
    client.post(f"/projects/{project_id}/tasks", data={"title": "Open thing"})
    client.post(f"/tasks/{done_id}/toggle")

    text = pdf_text(client.get("/export.pdf").content)
    assert "[x] Done thing" in text
    assert "[ ] Open thing" in text


def test_unknown_format_still_rejected(client):
    resp = client.get("/export.xml")
    assert resp.status_code == 404
    assert "pdf" in resp.text


def test_pdf_for_missing_project_404(client):
    assert client.get("/export.pdf?project_id=999").status_code == 404


def test_long_content_does_not_break_rendering(client):
    project_id = seed(client, with_data=False)
    client.post(
        f"/projects/{project_id}/tasks",
        data={"title": "T " * 120, "notes": "note " * 200},
    )
    resp = client.get("/export.pdf")
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")


def test_cli_export_pdf_writes_file(run, tmp_path):
    run("add-project", "Apollo", "--purpose", "Land it")
    run("add-task", "1", "Write spec")

    target = tmp_path / "out.pdf"
    code, out, _ = run("export", "pdf", "--out", str(target))
    assert code == 0
    assert "Wrote" in out
    assert target.read_bytes().startswith(b"%PDF-")
    assert "Write spec" in pdf_text(target.read_bytes())


def test_cli_export_pdf_defaults_to_a_file(run, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run("add-project", "Apollo")
    code, out, _ = run("export", "pdf")
    assert code == 0
    assert (tmp_path / "projects.pdf").read_bytes().startswith(b"%PDF-")


def test_cli_export_pdf_scoped_to_project(run, tmp_path):
    run("add-project", "Apollo")
    run("add-project", "Gemini")
    target = tmp_path / "one.pdf"
    run("export", "pdf", "--project-id", "1", "--out", str(target))

    text = pdf_text(target.read_bytes())
    assert "Apollo" in text
    assert "Gemini" not in text
