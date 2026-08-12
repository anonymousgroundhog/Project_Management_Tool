"""FastAPI app serving an HTMX-driven project/task tracker."""

import csv
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import services
from app.db import get_session, init_db
from app.models import PRIORITIES, PROJECT_STATUSES, TASK_STATUSES, format_duration

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Project Management Tool", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals.update(
    project_statuses=PROJECT_STATUSES,
    task_statuses=TASK_STATUSES,
    priorities=PRIORITIES,
    format_duration=format_duration,
)


@app.exception_handler(services.DomainError)
async def domain_error_handler(request: Request, exc: services.DomainError):
    return Response(str(exc), status_code=400)


@app.exception_handler(LookupError)
async def lookup_error_handler(request: Request, exc: LookupError):
    return Response(str(exc), status_code=404)


def load_project(session: Session, project_id: int):
    try:
        return services.get_project(session, project_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="project not found")


def load_task(session: Session, task_id: int):
    try:
        return services.get_task(session, task_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="task not found")


def load_member(session: Session, member_id: int):
    try:
        return services.get_member(session, member_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="member not found")


def task_row(request: Request, task) -> HTMLResponse:
    return templates.TemplateResponse(request, "_task_row.html", {"task": task})


# --- projects ---------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    status: str = "",
    session: Session = Depends(get_session),
):
    projects = services.list_projects(session, status)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "projects": projects,
            "active_filter": status,
            "running": services.running_timer(session),
        },
    )


@app.post("/projects")
def create_project(
    name: str = Form(...),
    purpose: str = Form(""),
    session: Session = Depends(get_session),
):
    project = services.create_project(session, name, purpose)
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(
    request: Request,
    project_id: int,
    session: Session = Depends(get_session),
):
    project = load_project(session, project_id)
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "project": project,
            "running": services.running_timer(session),
            "all_members": services.all_members(session),
        },
    )


@app.post("/projects/{project_id}/edit")
def edit_project(
    project_id: int,
    name: str = Form(...),
    purpose: str = Form(""),
    status: str = Form("active"),
    session: Session = Depends(get_session),
):
    project = load_project(session, project_id)
    services.update_project(
        session, project, name=name, purpose=purpose, status=status
    )
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


@app.post("/projects/{project_id}/delete")
def delete_project(project_id: int, session: Session = Depends(get_session)):
    services.delete_project(session, load_project(session, project_id))
    return RedirectResponse("/", status_code=303)


# --- members ----------------------------------------------------------------


@app.get("/members", response_class=HTMLResponse)
def members_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request,
        "members.html",
        {
            "members": services.all_members(session),
            "running": services.running_timer(session),
        },
    )


@app.post("/members")
def create_member(
    name: str = Form(...),
    email: str = Form(""),
    role: str = Form(""),
    session: Session = Depends(get_session),
):
    services.create_member(session, name, email, role)
    return RedirectResponse("/members", status_code=303)


@app.post("/members/{member_id}/edit")
def edit_member(
    member_id: int,
    name: str = Form(...),
    email: str = Form(""),
    role: str = Form(""),
    session: Session = Depends(get_session),
):
    member = load_member(session, member_id)
    services.update_member(session, member, name=name, email=email, role=role)
    return RedirectResponse("/members", status_code=303)


@app.post("/members/{member_id}/delete")
def delete_member(member_id: int, session: Session = Depends(get_session)):
    services.delete_member(session, load_member(session, member_id))
    return RedirectResponse("/members", status_code=303)


@app.post("/projects/{project_id}/members", response_class=HTMLResponse)
def add_project_member(
    request: Request,
    project_id: int,
    name: str = Form(...),
    session: Session = Depends(get_session),
):
    """Add someone to a project, creating the member record if they are new."""
    project = load_project(session, project_id)
    member = services.get_or_create_member(session, name)
    services.add_member_to_project(session, project, member)
    session.refresh(project)
    return templates.TemplateResponse(
        request,
        "_team.html",
        {"project": project, "all_members": services.all_members(session)},
    )


@app.post("/projects/{project_id}/members/{member_id}/remove", response_class=HTMLResponse)
def remove_project_member(
    request: Request,
    project_id: int,
    member_id: int,
    session: Session = Depends(get_session),
):
    project = load_project(session, project_id)
    services.remove_member_from_project(
        session, project, load_member(session, member_id)
    )
    session.refresh(project)
    return templates.TemplateResponse(
        request,
        "_team.html",
        {"project": project, "all_members": services.all_members(session)},
    )


@app.post("/tasks/{task_id}/assign", response_class=HTMLResponse)
def assign_task(
    request: Request,
    task_id: int,
    assignee: list[str] = Form(default=[]),
    session: Session = Depends(get_session),
):
    """Replace the task's assignees with the submitted set, which may be empty."""
    task = load_task(session, task_id)
    members = services.resolve_assignees(session, task.project, assignee)
    services.set_task_assignees(session, task, members)
    session.refresh(task)
    return task_row(request, task.parent or task)


@app.post("/tasks/{task_id}/assign/{member_id}", response_class=HTMLResponse)
def add_task_assignee(
    request: Request,
    task_id: int,
    member_id: int,
    session: Session = Depends(get_session),
):
    task = load_task(session, task_id)
    services.add_task_assignee(session, task, load_member(session, member_id))
    session.refresh(task)
    return task_row(request, task.parent or task)


@app.post("/tasks/{task_id}/unassign/{member_id}", response_class=HTMLResponse)
def remove_task_assignee(
    request: Request,
    task_id: int,
    member_id: int,
    session: Session = Depends(get_session),
):
    task = load_task(session, task_id)
    services.remove_task_assignee(session, task, load_member(session, member_id))
    session.refresh(task)
    return task_row(request, task.parent or task)


# --- search -----------------------------------------------------------------


@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: str = "",
    tag: str = "",
    status: str = "",
    priority: str = "",
    assignee: str = "",
    overdue: str = "",
    session: Session = Depends(get_session),
):
    tasks = services.search_tasks(
        session,
        q,
        tag=tag,
        status=status,
        priority=priority,
        assignee=assignee,
        overdue_only=bool(overdue),
    )
    context = {
        "tasks": tasks,
        "q": q,
        "tag": tag,
        "status": status,
        "priority": priority,
        "assignee": assignee,
        "overdue": bool(overdue),
        "tags": services.all_tags(session),
        "members": services.all_members(session),
        "running": services.running_timer(session),
    }
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_results.html", context)
    return templates.TemplateResponse(request, "search.html", context)


# --- tasks ------------------------------------------------------------------


@app.post("/projects/{project_id}/tasks", response_class=HTMLResponse)
def create_task(
    request: Request,
    project_id: int,
    title: str = Form(...),
    notes: str = Form(""),
    priority: str = Form("med"),
    due: str = Form(""),
    tags: str = Form(""),
    estimate: str = Form(""),
    assignee: list[str] = Form(default=[]),
    session: Session = Depends(get_session),
):
    project = load_project(session, project_id)
    task = services.create_task(
        session,
        project,
        title=title,
        notes=notes,
        priority=priority,
        due=due,
        tags=tags,
        estimate=estimate,
        assignee=assignee,
    )
    return task_row(request, task)


@app.post("/tasks/{task_id}/subtasks", response_class=HTMLResponse)
def create_subtask(
    request: Request,
    task_id: int,
    title: str = Form(...),
    priority: str = Form("med"),
    due: str = Form(""),
    assignee: list[str] = Form(default=[]),
    session: Session = Depends(get_session),
):
    parent = load_task(session, task_id)
    services.create_task(
        session,
        parent.project,
        title=title,
        priority=priority,
        due=due,
        assignee=assignee,
        parent=parent,
    )
    session.refresh(parent)
    return task_row(request, parent)


@app.post("/tasks/{task_id}/edit", response_class=HTMLResponse)
def edit_task(
    request: Request,
    task_id: int,
    title: str = Form(...),
    notes: str = Form(""),
    status: str = Form("todo"),
    priority: str = Form("med"),
    due: str = Form(""),
    tags: str = Form(""),
    estimate: str = Form(""),
    assignee: list[str] = Form(default=[]),
    session: Session = Depends(get_session),
):
    task = load_task(session, task_id)
    services.update_task(
        session,
        task,
        title=title,
        notes=notes,
        status=status,
        priority=priority,
        due=due,
        tags=tags,
        estimate=estimate,
        assignee=assignee,
    )
    return task_row(request, task.parent or task)


@app.post("/tasks/{task_id}/toggle", response_class=HTMLResponse)
def toggle_task(
    request: Request,
    task_id: int,
    session: Session = Depends(get_session),
):
    task = load_task(session, task_id)
    services.toggle_task(session, task)
    return task_row(request, task.parent or task)


@app.post("/tasks/{task_id}/timer/start", response_class=HTMLResponse)
def timer_start(
    request: Request,
    task_id: int,
    session: Session = Depends(get_session),
):
    task = load_task(session, task_id)
    services.start_timer(session, task)
    session.refresh(task)
    return task_row(request, task.parent or task)


@app.post("/tasks/{task_id}/timer/stop", response_class=HTMLResponse)
def timer_stop(
    request: Request,
    task_id: int,
    session: Session = Depends(get_session),
):
    task = load_task(session, task_id)
    services.stop_timer(session, task)
    session.refresh(task)
    return task_row(request, task.parent or task)


@app.post("/tasks/{task_id}/timer/log", response_class=HTMLResponse)
def timer_log(
    request: Request,
    task_id: int,
    minutes: str = Form(...),
    note: str = Form(""),
    session: Session = Depends(get_session),
):
    task = load_task(session, task_id)
    services.log_time(session, task, services.parse_estimate(minutes) or 0, note)
    session.refresh(task)
    return task_row(request, task.parent or task)


@app.get("/tasks/{task_id}/edit", response_class=HTMLResponse)
def task_edit_form(
    request: Request,
    task_id: int,
    session: Session = Depends(get_session),
):
    task = load_task(session, task_id)
    return templates.TemplateResponse(request, "_task_edit.html", {"task": task})


@app.get("/tasks/{task_id}/row", response_class=HTMLResponse)
def task_row_view(
    request: Request,
    task_id: int,
    session: Session = Depends(get_session),
):
    return task_row(request, load_task(session, task_id))


@app.delete("/tasks/{task_id}")
def delete_task(task_id: int, session: Session = Depends(get_session)):
    services.delete_task(session, load_task(session, task_id))
    return Response(status_code=200)


# --- export -----------------------------------------------------------------


@app.get("/export.{fmt}")
def export(
    fmt: str,
    project_id: int | None = None,
    session: Session = Depends(get_session),
):
    project = load_project(session, project_id) if project_id is not None else None
    stem = "projects" if project is None else f"project-{project_id}"

    if fmt == "pdf":
        from app.pdf import render_pdf

        body = render_pdf(
            services.export_payload(session, project_id),
            heading=project.name if project else "Projects",
        )
        return Response(
            body,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{stem}.pdf"'},
        )

    if fmt == "json":
        body = json.dumps(services.export_payload(session, project_id), indent=2)
        media = "application/json"
    elif fmt == "csv":
        buffer = io.StringIO()
        csv.writer(buffer).writerows(services.export_csv_rows(session, project_id))
        body = buffer.getvalue()
        media = "text/csv"
    elif fmt == "md":
        body = services.export_markdown(session, project_id)
        media = "text/markdown"
    else:
        raise HTTPException(
            status_code=404, detail="format must be pdf, json, csv, or md"
        )

    return Response(
        body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{stem}.{fmt}"'},
    )
