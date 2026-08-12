# Project Management Tool

Local single-user tracker for projects, their purpose, and their tasks.
FastAPI + SQLite + HTMX, no build step and no external CDN calls.
A CLI drives the same database as the web UI.

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000

The SQLite file `pmtool.db` is created next to this README on first start.
Set `PMTOOL_DB=/path/to/other.db` to use a different file.

## Schema changes

`init_db()` runs on startup: it creates any missing tables, then adds columns
listed in `ADDED_COLUMNS` in `app/db.py` that an older database is missing.
It is idempotent and leaves existing rows alone — new columns come back NULL,
which is what a task with no parent and no estimate looks like.

When you add a column to a model, add it to `ADDED_COLUMNS` too, otherwise
databases created before your change keep the old table shape and queries
fail with `no such column`. Anything beyond adding nullable columns (renames,
drops, type changes) needs a real migration tool such as Alembic.

## Features

- **Projects** with a stated purpose, status, and progress bar.
- **Tasks** with status, priority, due date, notes, and an optional estimate.
- **Subtasks** one level deep. Completing a parent completes its children.
- **Tags** — free-form, normalized to slugs (`API, Docs` becomes `#api #docs`).
  Tags with no remaining tasks are pruned.
- **Team members** — people are shared across projects. Put someone on a
  project from that project's page, then assign them to its work. A task or
  subtask can have **several assignees**, and everyone assigned must already be
  on that task's project.
- **Search** across task titles, notes, and project names, filtered by tag,
  status, priority, assignee (or `unassigned`), or overdue-only. Results update
  as you type. A task matches if *any* of its assignees match.
- **Time tracking** — start/stop a timer, or log time after the fact
  (`45`, `1h30m`). Timing is **cumulative**: each start/stop is its own
  session, and stopping then starting again resumes from the running total
  rather than restarting at zero. Time between sessions is not counted. Only
  one timer runs at a time; starting a second stops the first and banks its
  time. Totals roll up from subtasks to tasks to projects.
- **Export** to PDF, Markdown, CSV, or JSON, for everything or one project.
  The PDF is a printable report: one project per page, with the purpose, team,
  progress, and a task table showing subtasks, tags, assignees, due dates, and
  tracked-versus-estimated time.

## Data model

- **Project** — `name`, `purpose` (why the project exists), `status`
  (`active`, `on_hold`, `done`, `archived`), `created_at`.
- **Task** — belongs to one project, optionally to a parent task; `title`,
  `notes`, `status` (`todo`, `doing`, `blocked`, `done`), `priority`
  (`low`, `med`, `high`), `due`, `estimate_minutes`, `created_at`.
- **Tag** — unique slug, many-to-many with tasks.
- **Member** — a person; unique `name`, plus `email` and `role`. Many-to-many
  with projects (`project_members`) and with tasks (`task_assignees`).
- **TimeEntry** — belongs to a task; `started_at`, `ended_at` (null while
  running), `note`.

Deleting a project deletes its tasks; deleting a task deletes its subtasks and
time entries. Deleting a person, or taking them off a project, leaves the tasks
alone and only drops that person's assignment — anyone else assigned to the
same task keeps theirs.

Tasks previously held a single `assignee_id` column. That column is still
created for upgrades and backfilled into `task_assignees` on startup, then left
empty and unused; the join table is the only source of truth.

## CLI

```bash
python -m app.cli --help

python -m app.cli add-project "Apollo" --purpose "Land it"
python -m app.cli add-task 1 "Write spec" --tags "api docs" --due 2030-05-01 --estimate 1h30m
python -m app.cli add-task 1 "Draft outline" --parent 1     # subtask
python -m app.cli show 1

# people and assignment
python -m app.cli add-member "Ada" --role lead --email ada@example.com
python -m app.cli team-add 1 "Ada"          # creates the person if new
python -m app.cli add-task 1 "Pair work" --assignee Ada --assignee Grace
python -m app.cli assign 1 Grace            # adds, keeping existing assignees
python -m app.cli assign 1 Ada --replace    # sets the list to exactly Ada
python -m app.cli unassign 1 Ada            # removes one person
python -m app.cli unassign 1                # removes everyone
python -m app.cli members

python -m app.cli search spec --tag api --overdue
python -m app.cli search --assignee Ada     # or --assignee unassigned
python -m app.cli start 1        # timer on
python -m app.cli status         # what's running
python -m app.cli stop
python -m app.cli log 1 45 --note "reviewed the brief"
python -m app.cli done 1
python -m app.cli export md --out backlog.md
python -m app.cli export pdf --out status.pdf   # binary, always writes a file
python -m app.cli export pdf --project-id 1
```

## Export from the web

- `/export.pdf`, `/export.md`, `/export.csv`, `/export.json`
- add `?project_id=N` to scope the export to one project

PDFs are drawn with reportlab, a pure-Python dependency, so exporting needs no
system tools such as wkhtmltopdf. The standard PDF fonts only cover Latin-1, so
the report sticks to ASCII markers (`[ ]`, `[x]`) rather than box or tick
characters, which would otherwise render as filled squares.

## Layout

```
app/
  main.py        HTTP routes
  services.py    domain logic shared by the web app, CLI, and exporters
  models.py      ORM models
  db.py          engine, session, schema bootstrap
  pdf.py         PDF rendering of the export payload
  cli.py         command line front end
  templates/     Jinja templates (files starting with _ are HTMX fragments)
  static/        style.css, vendored htmx.min.js
tests/
  test_app.py        route tests
  test_cli.py        CLI tests
  test_migration.py  upgrading an older database
  test_members.py    team members and assignment
  test_multi_assignee.py  several assignees per task
  test_timing.py     cumulative timing across sessions
  test_pdf.py        PDF export
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

`requirements-dev.txt` adds pytest, httpx, and pypdf. pypdf is only used by the
tests, to read exported PDFs back and check what a reader would see.

## Scope note

No authentication and no CSRF protection. Bind to localhost only; do not
expose this to a network as-is.
