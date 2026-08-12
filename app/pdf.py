"""PDF rendering of the export payload, built with reportlab.

Kept separate from services.py so the rest of the app has no dependency on a
PDF library, and so the layout can change without touching domain logic.
"""

from datetime import datetime, timezone
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.models import format_duration

ACCENT = colors.HexColor("#3b6ef5")
INK = colors.HexColor("#16181d")
MUTED = colors.HexColor("#6b7280")
LINE = colors.HexColor("#d8dce3")
DANGER = colors.HexColor("#b3261e")
DONE = colors.HexColor("#2e7d32")

COLUMN_WIDTHS = (64 * mm, 17 * mm, 16 * mm, 30 * mm, 21 * mm, 26 * mm)


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=base["Title"], fontSize=20, spaceAfter=2, textColor=INK,
            alignment=TA_LEFT,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"], fontSize=9, textColor=MUTED,
            spaceAfter=14,
        ),
        "project": ParagraphStyle(
            "project", parent=base["Heading1"], fontSize=14, textColor=INK,
            spaceBefore=6, spaceAfter=4,
        ),
        "purpose": ParagraphStyle(
            "purpose", parent=base["Normal"], fontSize=10, textColor=INK,
            spaceAfter=4, leading=14,
        ),
        "meta": ParagraphStyle(
            "meta", parent=base["Normal"], fontSize=8.5, textColor=MUTED,
            spaceAfter=8, leading=12,
        ),
        "cell": ParagraphStyle(
            "cell", parent=base["Normal"], fontSize=8.5, textColor=INK, leading=11,
        ),
        "cellmuted": ParagraphStyle(
            "cellmuted", parent=base["Normal"], fontSize=8.5, textColor=MUTED,
            leading=11,
        ),
        "header": ParagraphStyle(
            "header", parent=base["Normal"], fontSize=8, textColor=colors.white,
            leading=10,
        ),
        "empty": ParagraphStyle(
            "empty", parent=base["Normal"], fontSize=9, textColor=MUTED,
            spaceAfter=10,
        ),
    }


def _task_rows(task: dict, styles: dict, depth: int = 0) -> list[list]:
    """One table row per task, subtasks indented directly beneath their parent."""
    # Plain ASCII markers: the standard PDF fonts have no box or tick glyph,
    # and anything outside Latin-1 renders as a filled square.
    marker = "[x]" if task["status"] == "done" else "[ ]"
    indent = "&nbsp;" * (4 * depth)
    prefix = "- " if depth else ""
    title_style = styles["cellmuted"] if task["status"] == "done" else styles["cell"]

    title = f"{indent}{prefix}{marker} {task['title']}"
    if task["notes"]:
        title += f"<br/><font size='7' color='#6b7280'>{indent}{task['notes']}</font>"
    if task["tags"]:
        tags = " ".join(f"#{t}" for t in task["tags"])
        title += f"<br/><font size='7' color='#3b6ef5'>{indent}{tags}</font>"

    # Durations like "1h 30m" must not wrap between the hours and the minutes.
    def nbsp(value: str) -> str:
        return value.replace(" ", "&nbsp;")

    tracked = (
        nbsp(format_duration(task["tracked_seconds"])) if task["tracked_seconds"] else "-"
    )
    estimate = (
        nbsp(format_duration(task["estimate_minutes"] * 60))
        if task["estimate_minutes"]
        else "-"
    )
    assignees = ", ".join(task["assignees"]) if task["assignees"] else "-"

    rows = [
        [
            Paragraph(title, title_style),
            Paragraph(task["status"], styles["cellmuted"]),
            Paragraph(task["priority"], styles["cellmuted"]),
            Paragraph(assignees, styles["cellmuted"]),
            Paragraph(task["due"] or "-", styles["cellmuted"]),
            Paragraph(f"{tracked} / {estimate}", styles["cellmuted"]),
        ]
    ]
    for sub in task["subtasks"]:
        rows.extend(_task_rows(sub, styles, depth + 1))
    return rows


def _task_table(project: dict, styles: dict) -> Table:
    header = [
        Paragraph(text, styles["header"])
        for text in ("Task", "Status", "Priority", "Assignees", "Due", "Tracked / Est")
    ]
    data = [header]
    overdue_rows: list[int] = []
    done_rows: list[int] = []

    for task in project["tasks"]:
        for row in _task_rows(task, styles):
            data.append(row)

    # Mark rows for colouring by walking the same order the rows were built in.
    flat: list[dict] = []

    def collect(task: dict) -> None:
        flat.append(task)
        for sub in task["subtasks"]:
            collect(sub)

    for task in project["tasks"]:
        collect(task)

    today = datetime.now(timezone.utc).date().isoformat()
    for index, task in enumerate(flat, start=1):
        if task["status"] == "done":
            done_rows.append(index)
        elif task["due"] and task["due"] < today:
            overdue_rows.append(index)

    table = Table(data, colWidths=COLUMN_WIDTHS, repeatRows=1, hAlign="LEFT")
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f8fa")]),
    ]
    for index in done_rows:
        style.append(("TEXTCOLOR", (1, index), (1, index), DONE))
    for index in overdue_rows:
        style.append(("TEXTCOLOR", (4, index), (4, index), DANGER))
    table.setStyle(TableStyle(style))
    return table


def _project_flowables(project: dict, styles: dict) -> list:
    flowables = [Paragraph(project["name"], styles["project"])]

    purpose = project["purpose"] or "No purpose recorded."
    flowables.append(Paragraph(f"<b>Purpose:</b> {purpose}", styles["purpose"]))

    meta = [
        f"Status: {project['status'].replace('_', ' ')}",
        f"{project['progress']}% done",
        f"{project['open_tasks']} open",
        f"{format_duration(project['tracked_seconds'])} tracked",
    ]
    if project["members"]:
        team = ", ".join(
            f"{m['name']} ({m['role']})" if m["role"] else m["name"]
            for m in project["members"]
        )
        meta.append(f"Team: {team}")
    flowables.append(Paragraph(" &nbsp;·&nbsp; ".join(meta), styles["meta"]))

    if project["tasks"]:
        flowables.append(_task_table(project, styles))
    else:
        flowables.append(Paragraph("No tasks.", styles["empty"]))
    return flowables


def _page_furniture(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 12 * mm, "Project Tracker export")
    canvas.drawRightString(
        doc.pagesize[0] - 18 * mm, 12 * mm, f"Page {canvas.getPageNumber()}"
    )
    canvas.restoreState()


def render_pdf(payload: dict, heading: str = "Projects") -> bytes:
    """Turn an export payload into PDF bytes."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title=heading,
        author="Project Tracker",
    )
    styles = _styles()

    exported = payload["exported_at"]
    story: list = [
        Paragraph(heading, styles["title"]),
        Paragraph(f"Exported {exported[:19].replace('T', ' ')} UTC", styles["subtitle"]),
    ]

    projects = payload["projects"]
    if not projects:
        story.append(Paragraph("No projects to export.", styles["empty"]))

    for index, project in enumerate(projects):
        if index:
            story.append(PageBreak())
        flowables = _project_flowables(project, styles)
        # Keep a project's heading with at least the start of its table.
        story.append(KeepTogether(flowables[:3]))
        story.extend(flowables[3:])
        story.append(Spacer(1, 6))

    doc.build(story, onFirstPage=_page_furniture, onLaterPages=_page_furniture)
    return buffer.getvalue()
