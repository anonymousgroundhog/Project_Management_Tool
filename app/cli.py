"""Command line front end over the same database the web app uses.

    python -m app.cli projects
    python -m app.cli add-task 1 "Draft copy" --tags docs --due 2030-01-01
    python -m app.cli start 4
    python -m app.cli export md --out backlog.md
"""

import argparse
import csv
import json
import sys

from app import services
from app.db import SessionLocal, init_db
from app.models import format_duration


def _session():
    init_db()
    return SessionLocal()


# --- output helpers ---------------------------------------------------------


def print_projects(session, status: str) -> None:
    projects = services.list_projects(session, status)
    if not projects:
        print("No projects.")
        return
    for p in projects:
        tracked = f" · ⏱ {format_duration(p.tracked_seconds)}" if p.tracked_seconds else ""
        print(f"[{p.id}] {p.name}  ({p.status}, {p.progress}% done, {p.open_tasks} open{tracked})")
        if p.purpose:
            print(f"     purpose: {p.purpose}")


def print_task(task, indent: int = 0) -> None:
    box = "x" if task.status == "done" else " "
    bits = [task.priority, task.status]
    if task.assignees:
        bits.append(" ".join(f"@{m.name}" for m in task.assignees))
    if task.due:
        bits.append(f"due {task.due}")
    if task.tag_names:
        bits.append(" ".join(f"#{t}" for t in task.tag_names))
    if task.tracked_seconds:
        bits.append(f"⏱ {task.tracked_display}")
    if task.is_running:
        bits.append("RUNNING")
    pad = "  " * indent
    print(f"{pad}[{task.id}] [{box}] {task.title}  ({', '.join(bits)})")
    if task.notes:
        print(f"{pad}      {task.notes}")
    for sub in task.subtasks:
        print_task(sub, indent + 1)


# --- commands ---------------------------------------------------------------


def cmd_projects(args, session):
    print_projects(session, args.status)


def cmd_show(args, session):
    project = services.get_project(session, args.project_id)
    print(f"[{project.id}] {project.name} ({project.status})")
    print(f"Purpose: {project.purpose or '(none recorded)'}")
    if project.members:
        team = ", ".join(
            f"{m.name} ({m.role})" if m.role else m.name for m in project.members
        )
        print(f"Team: {team}")
    print(
        f"{project.progress}% done · {project.open_tasks} open · "
        f"⏱ {format_duration(project.tracked_seconds)} tracked"
    )
    print()
    if not project.root_tasks:
        print("No tasks.")
    for task in project.root_tasks:
        print_task(task)


def cmd_add_project(args, session):
    project = services.create_project(session, args.name, args.purpose)
    print(f"Created project {project.id}: {project.name}")


def cmd_add_task(args, session):
    project = services.get_project(session, args.project_id)
    parent = services.get_task(session, args.parent) if args.parent else None
    task = services.create_task(
        session,
        project,
        title=args.title,
        notes=args.notes,
        priority=args.priority,
        due=args.due,
        tags=args.tags,
        estimate=args.estimate,
        assignee=args.assignee,
        parent=parent,
    )
    print(f"Created task {task.id}: {task.title}")


def cmd_done(args, session):
    task = services.get_task(session, args.task_id)
    if task.status != "done":
        services.toggle_task(session, task)
    print(f"Task {task.id} is {task.status}")


def cmd_reopen(args, session):
    task = services.get_task(session, args.task_id)
    if task.status == "done":
        services.toggle_task(session, task)
    print(f"Task {task.id} is {task.status}")


def cmd_rm_task(args, session):
    task = services.get_task(session, args.task_id)
    title = task.title
    services.delete_task(session, task)
    print(f"Deleted task {args.task_id}: {title}")


def cmd_members(args, session):
    members = services.all_members(session)
    if not members:
        print("No people yet.")
        return
    for m in members:
        role = f", {m.role}" if m.role else ""
        email = f" <{m.email}>" if m.email else ""
        projects = ", ".join(p.name for p in m.projects) or "no projects"
        print(f"[{m.id}] {m.name}{email}{role}  ({m.open_task_count} open · {projects})")


def cmd_add_member(args, session):
    member = services.create_member(session, args.name, args.email, args.role)
    print(f"Created member {member.id}: {member.name}")


def cmd_rm_member(args, session):
    member = services.get_member(session, args.member_id)
    name = member.name
    services.delete_member(session, member)
    print(f"Deleted {name}; their tasks are now unassigned")


def cmd_team_add(args, session):
    project = services.get_project(session, args.project_id)
    member = services.get_or_create_member(session, args.name)
    services.add_member_to_project(session, project, member)
    print(f"{member.name} added to {project.name}")


def cmd_team_remove(args, session):
    project = services.get_project(session, args.project_id)
    member = services.find_member(session, args.name)
    if member is None:
        raise LookupError(f"no member named {args.name}")
    services.remove_member_from_project(session, project, member)
    print(f"{member.name} removed from {project.name}")


def cmd_assign(args, session):
    task = services.get_task(session, args.task_id)
    wanted = services.resolve_assignees(session, task.project, args.names)
    if args.replace:
        services.set_task_assignees(session, task, wanted)
    else:
        services.set_task_assignees(session, task, list(task.assignees) + wanted)
    who = ", ".join(task.assignee_names) or "nobody"
    print(f"Task {task.id} assigned to {who}")


def cmd_unassign(args, session):
    task = services.get_task(session, args.task_id)
    if args.names:
        for name in args.names:
            member = services.find_member(session, name)
            if member is None:
                raise LookupError(f"no member named {name}")
            services.remove_task_assignee(session, task, member)
        who = ", ".join(task.assignee_names) or "nobody"
        print(f"Task {task.id} assigned to {who}")
    else:
        services.set_task_assignees(session, task, [])
        print(f"Task {task.id} is unassigned")


def cmd_search(args, session):
    tasks = services.search_tasks(
        session,
        args.query,
        tag=args.tag,
        status=args.status,
        priority=args.priority,
        assignee=args.assignee,
        project_id=args.project_id,
        overdue_only=args.overdue,
    )
    if not tasks:
        print("No tasks match.")
        return
    for task in tasks:
        print(f"{task.project.name}:")
        print_task(task, indent=1)


def cmd_start(args, session):
    task = services.get_task(session, args.task_id)
    prior = task.tracked_seconds
    services.start_timer(session, task, args.note)
    line = f"Timer started on task {task.id}: {task.title}"
    if prior:
        line += f" (resuming from {format_duration(prior)})"
    print(line)


def cmd_stop(args, session):
    running = services.running_timer(session)
    if running is None:
        print("No timer running.")
        return
    task = running.task if args.task_id is None else services.get_task(session, args.task_id)
    entry = services.stop_timer(session, task)
    if entry is None:
        print(f"No timer running on task {task.id}.")
        return
    print(
        f"Stopped task {task.id} after {format_duration(entry.seconds)}"
        f" · {format_duration(task.tracked_seconds)} total"
    )


def cmd_log(args, session):
    task = services.get_task(session, args.task_id)
    minutes = services.parse_estimate(args.amount) or 0
    services.log_time(session, task, minutes, args.note)
    print(f"Logged {format_duration(minutes * 60)} on task {task.id}: {task.title}")


def cmd_status(args, session):
    running = services.running_timer(session)
    if running is None:
        print("No timer running.")
        return
    task = running.task
    line = (
        f"⏱ {format_duration(task.tracked_seconds)} total on task {task.id}: "
        f"{task.title}"
    )
    if task.own_seconds > running.seconds:
        line += f" (this session {format_duration(running.seconds)})"
    print(line)


def cmd_export(args, session):
    if args.format == "pdf":
        from app.pdf import render_pdf

        heading = "Projects"
        if args.project_id is not None:
            heading = services.get_project(session, args.project_id).name
        body = render_pdf(services.export_payload(session, args.project_id), heading)
        target = args.out or "projects.pdf"
        with open(target, "wb") as handle:
            handle.write(body)
        print(f"Wrote {target}")
        return

    if args.format == "json":
        body = json.dumps(services.export_payload(session, args.project_id), indent=2)
    elif args.format == "md":
        body = services.export_markdown(session, args.project_id)
    else:
        rows = services.export_csv_rows(session, args.project_id)
        if args.out:
            with open(args.out, "w", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerows(rows)
            print(f"Wrote {args.out}")
            return
        writer = csv.writer(sys.stdout)
        writer.writerows(rows)
        return

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(body)
        print(f"Wrote {args.out}")
    else:
        print(body)


# --- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pm", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("projects", help="list projects")
    p.add_argument("--status", default="", help="filter by project status")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("show", help="show one project and its tasks")
    p.add_argument("project_id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("add-project", help="create a project")
    p.add_argument("name")
    p.add_argument("--purpose", default="")
    p.set_defaults(func=cmd_add_project)

    p = sub.add_parser("add-task", help="create a task")
    p.add_argument("project_id", type=int)
    p.add_argument("title")
    p.add_argument("--notes", default="")
    p.add_argument("--priority", default="med", choices=["low", "med", "high"])
    p.add_argument("--due", default="", help="YYYY-MM-DD")
    p.add_argument("--tags", default="", help="space or comma separated")
    p.add_argument("--estimate", default="", help="minutes or 1h30m")
    p.add_argument(
        "--assignee",
        action="append",
        default=[],
        help="member name or id on the project; repeat for several people",
    )
    p.add_argument("--parent", type=int, help="parent task id, makes a subtask")
    p.set_defaults(func=cmd_add_task)

    p = sub.add_parser("done", help="mark a task done")
    p.add_argument("task_id", type=int)
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("reopen", help="mark a task not done")
    p.add_argument("task_id", type=int)
    p.set_defaults(func=cmd_reopen)

    p = sub.add_parser("rm-task", help="delete a task and its subtasks")
    p.add_argument("task_id", type=int)
    p.set_defaults(func=cmd_rm_task)

    p = sub.add_parser("search", help="search tasks")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--tag", default="")
    p.add_argument("--status", default="")
    p.add_argument("--priority", default="")
    p.add_argument("--assignee", default="", help="member name, or 'unassigned'")
    p.add_argument("--project-id", type=int, dest="project_id")
    p.add_argument("--overdue", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("members", help="list people")
    p.set_defaults(func=cmd_members)

    p = sub.add_parser("add-member", help="create a person")
    p.add_argument("name")
    p.add_argument("--email", default="")
    p.add_argument("--role", default="")
    p.set_defaults(func=cmd_add_member)

    p = sub.add_parser("rm-member", help="delete a person, unassigning their tasks")
    p.add_argument("member_id", type=int)
    p.set_defaults(func=cmd_rm_member)

    p = sub.add_parser("team-add", help="put someone on a project, creating them if new")
    p.add_argument("project_id", type=int)
    p.add_argument("name")
    p.set_defaults(func=cmd_team_add)

    p = sub.add_parser("team-remove", help="take someone off a project")
    p.add_argument("project_id", type=int)
    p.add_argument("name")
    p.set_defaults(func=cmd_team_remove)

    p = sub.add_parser(
        "assign", help="assign a task or subtask to one or more project members"
    )
    p.add_argument("task_id", type=int)
    p.add_argument("names", nargs="+", help="member names or ids")
    p.add_argument(
        "--replace",
        action="store_true",
        help="replace the current assignees instead of adding to them",
    )
    p.set_defaults(func=cmd_assign)

    p = sub.add_parser("unassign", help="remove assignees, or all of them")
    p.add_argument("task_id", type=int)
    p.add_argument("names", nargs="*", help="member names or ids; omit to clear all")
    p.set_defaults(func=cmd_unassign)

    p = sub.add_parser("start", help="start the timer on a task")
    p.add_argument("task_id", type=int)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", help="stop the running timer")
    p.add_argument("task_id", type=int, nargs="?")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("log", help="log time already spent")
    p.add_argument("task_id", type=int)
    p.add_argument("amount", help="minutes or 1h30m")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("status", help="show the running timer")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("export", help="export to pdf, json, csv, or md")
    p.add_argument("format", choices=["pdf", "json", "csv", "md"])
    p.add_argument("--project-id", type=int, dest="project_id")
    p.add_argument(
        "--out",
        help="write to this file instead of stdout; pdf is binary and always "
        "writes a file, defaulting to projects.pdf",
    )
    p.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session = _session()
    try:
        args.func(args, session)
    except (services.DomainError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
