"""ORM models for projects, tasks, tags, and time entries."""

import re
from datetime import date, datetime, timezone

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

PROJECT_STATUSES = ("active", "on_hold", "done", "archived")
TASK_STATUSES = ("todo", "doing", "blocked", "done")
PRIORITIES = ("low", "med", "high")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_tag(raw: str) -> str:
    """Lowercase, collapse whitespace/punctuation to single dashes."""
    slug = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    return slug[:40]


def parse_tag_list(raw: str) -> list[str]:
    """Turn a comma or space separated tag string into unique slugs, order kept."""
    seen: list[str] = []
    for chunk in re.split(r"[,\s]+", raw or ""):
        slug = normalize_tag(chunk)
        if slug and slug not in seen:
            seen.append(slug)
    return seen


class Base(DeclarativeBase):
    pass


task_tags = Table(
    "task_tags",
    Base.metadata,
    Column("task_id", ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)

task_assignees = Table(
    "task_assignees",
    Base.metadata,
    Column("task_id", ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    Column("member_id", ForeignKey("members.id", ondelete="CASCADE"), primary_key=True),
)

project_members = Table(
    "project_members",
    Base.metadata,
    Column(
        "project_id", ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    ),
    Column(
        "member_id", ForeignKey("members.id", ondelete="CASCADE"), primary_key=True
    ),
)


class Member(Base):
    """A person. Members are global so one person can be on several projects."""

    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("name", name="uq_member_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    projects: Mapped[list["Project"]] = relationship(
        secondary=project_members, back_populates="members", order_by="Project.name"
    )
    tasks: Mapped[list["Task"]] = relationship(
        secondary=task_assignees, back_populates="assignees", order_by="Task.id"
    )

    @property
    def initials(self) -> str:
        parts = [p for p in self.name.split() if p]
        letters = "".join(p[0] for p in parts[:2])
        return (letters or self.name[:2]).upper()

    @property
    def open_task_count(self) -> int:
        return sum(1 for t in self.tasks if t.status != "done")

    def __str__(self) -> str:
        return self.name


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("name", name="uq_tag_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

    tasks: Mapped[list["Task"]] = relationship(
        secondary=task_tags, back_populates="tags"
    )

    def __str__(self) -> str:
        return self.name


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tasks: Mapped[list["Task"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="Task.id",
    )
    members: Mapped[list[Member]] = relationship(
        secondary=project_members, back_populates="projects", order_by="Member.name"
    )

    @property
    def unassigned_tasks(self) -> int:
        return sum(1 for t in self.tasks if not t.assignees)

    @property
    def root_tasks(self) -> list["Task"]:
        return [t for t in self.tasks if t.parent_id is None]

    @property
    def open_tasks(self) -> int:
        return sum(1 for t in self.tasks if t.status != "done")

    @property
    def progress(self) -> int:
        """Percent of tasks done; 0 when the project has no tasks yet."""
        if not self.tasks:
            return 0
        done = sum(1 for t in self.tasks if t.status == "done")
        return round(done * 100 / len(self.tasks))

    @property
    def tracked_seconds(self) -> int:
        return sum(t.tracked_seconds for t in self.tasks)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="todo")
    priority: Mapped[str] = mapped_column(String(10), default="med")
    due: Mapped[date | None] = mapped_column(Date, nullable=True)
    estimate_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="tasks")
    assignees: Mapped[list[Member]] = relationship(
        secondary=task_assignees, back_populates="tasks", order_by="Member.name"
    )
    parent: Mapped["Task | None"] = relationship(
        back_populates="subtasks", remote_side="Task.id"
    )
    subtasks: Mapped[list["Task"]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
        order_by="Task.id",
        single_parent=True,
    )
    tags: Mapped[list[Tag]] = relationship(
        secondary=task_tags, back_populates="tasks", order_by="Tag.name"
    )
    time_entries: Mapped[list["TimeEntry"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TimeEntry.id",
    )

    @property
    def overdue(self) -> bool:
        return bool(self.due and self.status != "done" and self.due < date.today())

    @property
    def tag_names(self) -> list[str]:
        return [t.name for t in self.tags]

    @property
    def assignee_names(self) -> list[str]:
        return [m.name for m in self.assignees]

    @property
    def assignee_ids(self) -> set[int]:
        return {m.id for m in self.assignees}

    @property
    def is_assigned(self) -> bool:
        return bool(self.assignees)

    @property
    def assignee_label(self) -> str:
        """Short form for one-line displays: '@Ada', or '@Ada +2' for a group."""
        if not self.assignees:
            return ""
        first = self.assignees[0].name
        extra = len(self.assignees) - 1
        return f"@{first}" + (f" +{extra}" if extra else "")

    @property
    def running_entry(self) -> "TimeEntry | None":
        for entry in self.time_entries:
            if entry.ended_at is None:
                return entry
        return None

    @property
    def is_running(self) -> bool:
        return self.running_entry is not None

    @property
    def own_seconds(self) -> int:
        """Time logged against this task alone, excluding its subtasks."""
        return sum(e.seconds for e in self.time_entries)

    @property
    def tracked_seconds(self) -> int:
        """Logged time for this task and its subtasks, including a running timer.

        Timing a task is cumulative: stopping and starting again resumes from
        the running total rather than restarting at zero, because every session
        is its own TimeEntry and they all count here. Time between sessions is
        not counted.
        """
        return self.own_seconds + sum(s.tracked_seconds for s in self.subtasks)

    @property
    def tracked_display(self) -> str:
        return format_duration(self.tracked_seconds)

    @property
    def session_seconds(self) -> int:
        """Elapsed time of the current session only, 0 when nothing is running."""
        entry = self.running_entry
        return entry.seconds if entry else 0

    @property
    def subtask_progress(self) -> str:
        if not self.subtasks:
            return ""
        done = sum(1 for s in self.subtasks if s.status == "done")
        return f"{done}/{len(self.subtasks)}"


class TimeEntry(Base):
    __tablename__ = "time_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    task: Mapped[Task] = relationship(back_populates="time_entries")

    @property
    def seconds(self) -> int:
        """Elapsed seconds; a running entry counts up to now."""
        start = _as_utc(self.started_at)
        end = _as_utc(self.ended_at) if self.ended_at else utcnow()
        return max(0, int((end - start).total_seconds()))


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat those as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0m"
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m" if minutes else "<1m"
