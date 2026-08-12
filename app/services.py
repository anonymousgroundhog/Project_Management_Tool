"""Domain logic shared by the web routes, the CLI, and the exporters."""

import re
from datetime import date, datetime, timedelta
from typing import Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    PRIORITIES,
    PROJECT_STATUSES,
    TASK_STATUSES,
    Member,
    Project,
    Tag,
    Task,
    TimeEntry,
    format_duration,
    parse_tag_list,
    utcnow,
)


class DomainError(ValueError):
    """Invalid input from any front end. Routes map this to HTTP 400."""


def check_choice(value: str, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise DomainError(f"{field} must be one of {', '.join(allowed)}")
    return value


def parse_due(value: str | date | None) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DomainError("due must be YYYY-MM-DD")


def parse_estimate(value: str | int | None) -> int | None:
    """Accept plain minutes ('90') or an h/m form ('1h30m', '2h', '45m')."""
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip().lower().replace(" ", "")
    if text.isdigit():
        minutes = int(text)
    else:
        match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", text)
        if not match or not any(match.groups()):
            raise DomainError("estimate must be minutes or a form like 1h30m")
        hours, mins = match.groups()
        minutes = int(hours or 0) * 60 + int(mins or 0)
    if minutes <= 0:
        raise DomainError("estimate must be greater than zero")
    return minutes


# --- lookups ----------------------------------------------------------------


def get_project(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError("project not found")
    return project


def get_task(session: Session, task_id: int) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise LookupError("task not found")
    return task


def get_member(session: Session, member_id: int) -> Member:
    member = session.get(Member, member_id)
    if member is None:
        raise LookupError("member not found")
    return member


def list_projects(session: Session, status: str = "") -> list[Project]:
    stmt = select(Project).options(
        selectinload(Project.tasks).selectinload(Task.time_entries),
        selectinload(Project.tasks).selectinload(Task.subtasks),
        selectinload(Project.members),
    )
    if status:
        stmt = stmt.where(Project.status == status)
    else:
        stmt = stmt.where(Project.status != "archived")
    return list(session.scalars(stmt.order_by(Project.created_at.desc())).unique())


def all_tags(session: Session) -> list[Tag]:
    return list(session.scalars(select(Tag).order_by(Tag.name)))


# --- tags -------------------------------------------------------------------


def resolve_tags(session: Session, names: Iterable[str]) -> list[Tag]:
    """Fetch or create tags by slug, reusing rows so names stay unique."""
    resolved: list[Tag] = []
    for name in names:
        tag = session.scalar(select(Tag).where(Tag.name == name))
        if tag is None:
            tag = Tag(name=name)
            session.add(tag)
            session.flush()
        resolved.append(tag)
    return resolved


def prune_orphan_tags(session: Session) -> None:
    """Drop tags no task references any more, so filters stay meaningful."""
    for tag in session.scalars(select(Tag)):
        if not tag.tasks:
            session.delete(tag)


# --- members ----------------------------------------------------------------


def all_members(session: Session) -> list[Member]:
    return list(
        session.scalars(
            select(Member).options(selectinload(Member.tasks)).order_by(Member.name)
        ).unique()
    )


def find_member(session: Session, name: str) -> Member | None:
    """Look a member up by exact id-as-string or case-insensitive name."""
    text = (name or "").strip()
    if not text:
        return None
    if text.isdigit():
        return session.get(Member, int(text))
    return session.scalar(
        select(Member).where(func.lower(Member.name) == text.lower())
    )


def create_member(
    session: Session, name: str, email: str = "", role: str = ""
) -> Member:
    name = (name or "").strip()
    if not name:
        raise DomainError("member name required")
    if find_member(session, name) is not None:
        raise DomainError(f"a member named {name} already exists")
    member = Member(name=name, email=(email or "").strip(), role=(role or "").strip())
    session.add(member)
    session.commit()
    return member


def update_member(
    session: Session, member: Member, *, name: str, email: str = "", role: str = ""
) -> Member:
    name = (name or "").strip()
    if not name:
        raise DomainError("member name required")
    clash = find_member(session, name)
    if clash is not None and clash.id != member.id:
        raise DomainError(f"a member named {name} already exists")
    member.name = name
    member.email = (email or "").strip()
    member.role = (role or "").strip()
    session.commit()
    return member


def delete_member(session: Session, member: Member) -> None:
    """Remove a member. Their tasks stay put, minus this person's assignment."""
    for task in list(member.tasks):
        task.assignees = [m for m in task.assignees if m.id != member.id]
    member.projects.clear()
    session.delete(member)
    session.commit()


def get_or_create_member(session: Session, name: str) -> Member:
    member = find_member(session, name)
    if member is not None:
        return member
    return create_member(session, name)


def add_member_to_project(
    session: Session, project: Project, member: Member
) -> Member:
    if member not in project.members:
        project.members.append(member)
        session.commit()
    return member


def remove_member_from_project(
    session: Session, project: Project, member: Member
) -> None:
    """Take someone off a project and drop their assignments on it.

    Other people assigned to the same tasks keep their assignments.
    """
    if member in project.members:
        project.members.remove(member)
    for task in project.tasks:
        if member.id in task.assignee_ids:
            task.assignees = [m for m in task.assignees if m.id != member.id]
    session.commit()


def set_task_assignees(
    session: Session,
    task: Task,
    members: Iterable[Member],
    commit: bool = True,
) -> Task:
    """Replace a task's assignees. Everyone named must be on the project."""
    chosen: list[Member] = []
    for member in members:
        if member not in task.project.members:
            raise DomainError(
                f"{member.name} is not a member of {task.project.name}; "
                "add them to the project first"
            )
        if member not in chosen:
            chosen.append(member)
    task.assignees = chosen
    if commit:
        session.commit()
    return task


def add_task_assignee(session: Session, task: Task, member: Member) -> Task:
    """Add one person without disturbing the others already on the task."""
    return set_task_assignees(session, task, list(task.assignees) + [member])


def remove_task_assignee(session: Session, task: Task, member: Member) -> Task:
    return set_task_assignees(
        session, task, [m for m in task.assignees if m.id != member.id]
    )


def parse_assignee_list(raw: str | int | None) -> list[str]:
    """Split a comma separated assignee string into names, order preserved.

    Names may contain spaces, so only commas separate them. Empty values and
    the word 'none' mean nobody.
    """
    if raw in (None, "", "none"):
        return []
    if isinstance(raw, int):
        return [str(raw)]
    parts = [chunk.strip() for chunk in str(raw).split(",")]
    return [p for p in parts if p and p.lower() != "none"]


def resolve_assignees(
    session: Session, project: Project, value: str | int | Iterable[str] | None
) -> list[Member]:
    """Turn form values or CLI arguments into members on this project."""
    if value is None or isinstance(value, (str, int)):
        names = parse_assignee_list(value)
    else:
        names = [str(v).strip() for v in value if str(v).strip()]

    resolved: list[Member] = []
    for name in names:
        member = find_member(session, name)
        if member is None:
            raise DomainError(f"no member named {name}")
        if member not in project.members:
            raise DomainError(
                f"{member.name} is not a member of {project.name}; "
                "add them to the project first"
            )
        if member not in resolved:
            resolved.append(member)
    return resolved


# --- projects ---------------------------------------------------------------


def create_project(session: Session, name: str, purpose: str = "") -> Project:
    name = (name or "").strip()
    if not name:
        raise DomainError("name required")
    project = Project(name=name, purpose=(purpose or "").strip())
    session.add(project)
    session.commit()
    return project


def update_project(
    session: Session,
    project: Project,
    *,
    name: str,
    purpose: str,
    status: str,
) -> Project:
    name = (name or "").strip()
    if not name:
        raise DomainError("name required")
    project.name = name
    project.purpose = (purpose or "").strip()
    project.status = check_choice(status, PROJECT_STATUSES, "status")
    session.commit()
    return project


def delete_project(session: Session, project: Project) -> None:
    session.delete(project)
    prune_orphan_tags(session)
    session.commit()


# --- tasks ------------------------------------------------------------------


def create_task(
    session: Session,
    project: Project,
    *,
    title: str,
    notes: str = "",
    priority: str = "med",
    due: str | date | None = None,
    tags: str = "",
    estimate: str | int | None = None,
    assignee: str | int | Iterable[str] | None = None,
    parent: Task | None = None,
) -> Task:
    title = (title or "").strip()
    if not title:
        raise DomainError("title required")
    if parent is not None:
        if parent.project_id != project.id:
            raise DomainError("parent task belongs to a different project")
        if parent.parent_id is not None:
            raise DomainError("subtasks only nest one level deep")
    members = resolve_assignees(session, project, assignee)
    task = Task(
        project_id=project.id,
        parent_id=parent.id if parent else None,
        title=title,
        notes=(notes or "").strip(),
        priority=check_choice(priority, PRIORITIES, "priority"),
        due=parse_due(due),
        estimate_minutes=parse_estimate(estimate),
    )
    # Add the task before wiring up relationships, otherwise SQLAlchemy warns
    # that back-populating onto a member or tag has nothing to attach to.
    session.add(task)
    task.assignees = members
    task.tags = resolve_tags(session, parse_tag_list(tags))
    session.commit()
    return task


def update_task(
    session: Session,
    task: Task,
    *,
    title: str,
    notes: str = "",
    status: str = "todo",
    priority: str = "med",
    due: str | date | None = None,
    tags: str = "",
    estimate: str | int | None = None,
    assignee: str | int | Iterable[str] | None = None,
) -> Task:
    title = (title or "").strip()
    if not title:
        raise DomainError("title required")
    members = resolve_assignees(session, task.project, assignee)
    task.title = title
    task.notes = (notes or "").strip()
    task.status = check_choice(status, TASK_STATUSES, "status")
    task.priority = check_choice(priority, PRIORITIES, "priority")
    task.due = parse_due(due)
    task.estimate_minutes = parse_estimate(estimate)
    task.assignees = members
    task.tags = resolve_tags(session, parse_tag_list(tags))
    session.commit()
    prune_orphan_tags(session)
    session.commit()
    return task


def toggle_task(session: Session, task: Task) -> Task:
    """Flip done/todo. Completing a parent completes its subtasks too."""
    task.status = "todo" if task.status == "done" else "done"
    if task.status == "done":
        stop_timer(session, task, commit=False)
        for sub in task.subtasks:
            sub.status = "done"
            stop_timer(session, sub, commit=False)
    session.commit()
    return task


def delete_task(session: Session, task: Task) -> None:
    session.delete(task)
    session.commit()
    prune_orphan_tags(session)
    session.commit()


# --- search -----------------------------------------------------------------


def search_tasks(
    session: Session,
    query: str = "",
    *,
    tag: str = "",
    status: str = "",
    priority: str = "",
    assignee: str = "",
    project_id: int | None = None,
    overdue_only: bool = False,
    limit: int = 200,
) -> list[Task]:
    """Case-insensitive match on title/notes, narrowed by the given filters."""
    stmt = (
        select(Task)
        .join(Project)
        .options(
            selectinload(Task.tags),
            selectinload(Task.time_entries),
            selectinload(Task.subtasks),
            selectinload(Task.project),
            selectinload(Task.assignees),
        )
    )
    term = (query or "").strip()
    if term:
        like = f"%{term.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Task.title).like(like),
                func.lower(Task.notes).like(like),
                func.lower(Project.name).like(like),
            )
        )
    if tag:
        stmt = stmt.where(Task.tags.any(Tag.name == tag))
    if status:
        stmt = stmt.where(Task.status == check_choice(status, TASK_STATUSES, "status"))
    if priority:
        stmt = stmt.where(
            Task.priority == check_choice(priority, PRIORITIES, "priority")
        )
    if assignee:
        if assignee == "unassigned":
            stmt = stmt.where(~Task.assignees.any())
        else:
            member = find_member(session, assignee)
            if member is None:
                raise DomainError(f"no member named {assignee}")
            stmt = stmt.where(Task.assignees.any(Member.id == member.id))
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)
    if overdue_only:
        stmt = stmt.where(
            Task.due.is_not(None), Task.due < date.today(), Task.status != "done"
        )
    stmt = stmt.order_by(Task.due.is_(None), Task.due, Task.id.desc()).limit(limit)
    return list(session.scalars(stmt).unique())


# --- time tracking ----------------------------------------------------------


def start_timer(session: Session, task: Task, note: str = "") -> TimeEntry:
    """Start a timer, stopping any other running one so only one runs at a time."""
    running = session.scalar(select(TimeEntry).where(TimeEntry.ended_at.is_(None)))
    if running is not None:
        if running.task_id == task.id:
            return running
        running.ended_at = utcnow()
    entry = TimeEntry(task_id=task.id, note=(note or "").strip())
    session.add(entry)
    session.commit()
    return entry


def stop_timer(session: Session, task: Task, commit: bool = True) -> TimeEntry | None:
    entry = task.running_entry
    if entry is None:
        return None
    entry.ended_at = utcnow()
    if commit:
        session.commit()
    return entry


def log_time(session: Session, task: Task, minutes: int, note: str = "") -> TimeEntry:
    """Record a closed entry for work already done."""
    if minutes <= 0:
        raise DomainError("minutes must be greater than zero")
    ended = utcnow()
    entry = TimeEntry(
        task_id=task.id,
        started_at=ended - timedelta(minutes=minutes),
        ended_at=ended,
        note=(note or "").strip(),
    )
    session.add(entry)
    session.commit()
    return entry


def running_timer(session: Session) -> TimeEntry | None:
    return session.scalar(
        select(TimeEntry)
        .options(selectinload(TimeEntry.task))
        .where(TimeEntry.ended_at.is_(None))
    )


# --- export -----------------------------------------------------------------


def task_to_dict(task: Task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "notes": task.notes,
        "status": task.status,
        "priority": task.priority,
        "due": task.due.isoformat() if task.due else None,
        "estimate_minutes": task.estimate_minutes,
        "assignees": task.assignee_names,
        "tags": task.tag_names,
        "tracked_seconds": task.tracked_seconds,
        "tracked_display": task.tracked_display,
        "created_at": _iso(task.created_at),
        "time_entries": [
            {
                "id": e.id,
                "started_at": _iso(e.started_at),
                "ended_at": _iso(e.ended_at) if e.ended_at else None,
                "seconds": e.seconds,
                "note": e.note,
            }
            for e in task.time_entries
        ],
        "subtasks": [task_to_dict(s) for s in task.subtasks],
    }


def project_to_dict(project: Project) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "purpose": project.purpose,
        "status": project.status,
        "created_at": _iso(project.created_at),
        "progress": project.progress,
        "open_tasks": project.open_tasks,
        "tracked_seconds": project.tracked_seconds,
        "members": [
            {"id": m.id, "name": m.name, "email": m.email, "role": m.role}
            for m in project.members
        ],
        "tasks": [task_to_dict(t) for t in project.root_tasks],
    }


def export_payload(session: Session, project_id: int | None = None) -> dict:
    if project_id is None:
        stmt = select(Project).order_by(Project.created_at.desc())
        projects = list(session.scalars(stmt).unique())
    else:
        projects = [get_project(session, project_id)]
    return {
        "exported_at": _iso(utcnow()),
        "projects": [project_to_dict(p) for p in projects],
    }


def export_csv_rows(session: Session, project_id: int | None = None) -> list[list[str]]:
    """Flat one-row-per-task view, subtasks marked by their parent title."""
    payload = export_payload(session, project_id)
    rows = [
        [
            "project",
            "project_purpose",
            "project_status",
            "project_members",
            "parent_task",
            "task",
            "status",
            "priority",
            "assignees",
            "due",
            "estimate_minutes",
            "tags",
            "tracked_seconds",
            "notes",
        ]
    ]
    for project in payload["projects"]:
        for task in project["tasks"]:
            rows.append(_csv_row(project, "", task))
            for sub in task["subtasks"]:
                rows.append(_csv_row(project, task["title"], sub))
    return rows


def _csv_row(project: dict, parent_title: str, task: dict) -> list[str]:
    return [
        project["name"],
        project["purpose"],
        project["status"],
        "; ".join(m["name"] for m in project["members"]),
        parent_title,
        task["title"],
        task["status"],
        task["priority"],
        "; ".join(task["assignees"]),
        task["due"] or "",
        str(task["estimate_minutes"] or ""),
        " ".join(task["tags"]),
        str(task["tracked_seconds"]),
        task["notes"],
    ]


def export_markdown(session: Session, project_id: int | None = None) -> str:
    payload = export_payload(session, project_id)
    lines: list[str] = ["# Projects", ""]
    for project in payload["projects"]:
        lines.append(f"## {project['name']} ({project['status']})")
        lines.append("")
        lines.append(f"**Purpose:** {project['purpose'] or '_none recorded_'}")
        lines.append("")
        if project["members"]:
            team = ", ".join(
                f"{m['name']} ({m['role']})" if m["role"] else m["name"]
                for m in project["members"]
            )
            lines.append(f"**Team:** {team}")
            lines.append("")
        lines.append(
            f"{project['progress']}% done · {project['open_tasks']} open · "
            f"{format_duration(project['tracked_seconds'])} tracked"
        )
        lines.append("")
        if not project["tasks"]:
            lines.append("_No tasks._")
            lines.append("")
            continue
        for task in project["tasks"]:
            lines.append(_md_task(task, indent=0))
            for sub in task["subtasks"]:
                lines.append(_md_task(sub, indent=1))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _md_task(task: dict, indent: int) -> str:
    box = "[x]" if task["status"] == "done" else "[ ]"
    bits = [f"{'  ' * indent}- {box} {task['title']}"]
    meta = [task["priority"], task["status"]]
    if task["assignees"]:
        meta.append(" ".join(f"@{name}" for name in task["assignees"]))
    if task["due"]:
        meta.append(f"due {task['due']}")
    if task["tags"]:
        meta.append(" ".join(f"#{t}" for t in task["tags"]))
    if task["tracked_seconds"]:
        meta.append(task["tracked_display"])
    bits.append(f" _({', '.join(meta)})_")
    return "".join(bits)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
