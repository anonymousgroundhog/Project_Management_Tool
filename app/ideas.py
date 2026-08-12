"""Project idea generation from a topic.

Two backends. When an Anthropic API key is available the ideas come from
Claude; otherwise a template generator runs offline so the feature always
works. Both return the same shape, so callers never branch on the source.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

MAX_IDEAS = 8
DEFAULT_IDEAS = 5
MODEL = "claude-opus-5"


class IdeaError(Exception):
    """The idea generator could not produce ideas."""


@dataclass
class Idea:
    """One suggested project: what to build, why, and where to start."""

    name: str
    purpose: str
    tasks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"name": self.name, "purpose": self.purpose, "tasks": list(self.tasks)}


@dataclass
class IdeaSet:
    topic: str
    ideas: list[Idea]
    source: str  # "claude" or "offline"
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "topic": self.topic,
            "source": self.source,
            "note": self.note,
            "ideas": [i.as_dict() for i in self.ideas],
        }


def clean_topic(topic: str) -> str:
    topic = re.sub(r"\s+", " ", (topic or "").strip())
    if not topic:
        raise IdeaError("topic required")
    return topic[:120]


def clamp_count(count: int | str | None) -> int:
    if count in (None, ""):
        return DEFAULT_IDEAS
    try:
        value = int(count)
    except (TypeError, ValueError):
        raise IdeaError("count must be a number")
    return max(1, min(MAX_IDEAS, value))


def api_key_available() -> bool:
    """True when the SDK has some credential to work with.

    The SDK also reads ANTHROPIC_AUTH_TOKEN and `ant auth login` profiles, so
    an unset ANTHROPIC_API_KEY does not by itself mean there are no
    credentials. PMTOOL_OFFLINE_IDEAS forces the offline generator, which
    keeps tests deterministic and lets anyone opt out of network calls.
    """
    if os.environ.get("PMTOOL_OFFLINE_IDEAS"):
        return False
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    config = os.environ.get("ANTHROPIC_CONFIG_DIR") or os.path.expanduser(
        "~/.config/anthropic"
    )
    return os.path.isdir(os.path.join(config, "credentials"))


# --- offline generator ------------------------------------------------------

# Each archetype is a way of starting work on any topic: understand it, build
# the smallest version, make it reliable, and so on. Pairing the topic with an
# archetype gives a usable prompt rather than a genuinely novel idea.
ARCHETYPES: tuple[dict, ...] = (
    {
        "name": "{topic} audit and current-state map",
        "purpose": "Establish what already exists for {topic} before changing "
        "anything, so later work builds on facts rather than assumptions.",
        "tasks": [
            "List everything already in place for {topic}",
            "Note what works, what does not, and what is unknown",
            "Write up the three biggest gaps worth fixing",
        ],
    },
    {
        "name": "{topic} prototype: smallest useful version",
        "purpose": "Build the smallest version of {topic} that delivers real "
        "value, to test the idea before committing to a full build.",
        "tasks": [
            "Define the one job the prototype must do",
            "Cut every feature that is not that job",
            "Build it, then use it for a week and record what broke",
        ],
    },
    {
        "name": "{topic} reliability review",
        "purpose": "Find and fix the failure points that make {topic} "
        "untrustworthy day to day.",
        "tasks": [
            "Log every failure for two weeks",
            "Rank failures by how much they cost when they happen",
            "Fix the top three and add a check that catches regressions",
        ],
    },
    {
        "name": "{topic} cost and tooling review",
        "purpose": "Understand what {topic} actually costs in money and time, "
        "and decide what is worth keeping.",
        "tasks": [
            "Total the direct and recurring costs of {topic}",
            "Compare against two alternatives",
            "Recommend keep, replace, or drop, with reasons",
        ],
    },
    {
        "name": "{topic} documentation and onboarding guide",
        "purpose": "Write down how {topic} works so someone else can pick it "
        "up without a walkthrough.",
        "tasks": [
            "Draft a one-page overview of how {topic} works",
            "Document the three tasks people do most often",
            "Have someone unfamiliar follow it and note where they get stuck",
        ],
    },
    {
        "name": "{topic} automation of the repetitive parts",
        "purpose": "Remove the manual steps in {topic} that get repeated often "
        "enough to be worth automating.",
        "tasks": [
            "Track which {topic} steps get repeated each week",
            "Pick the one with the best time-saved-to-effort ratio",
            "Automate it and measure the time actually saved",
        ],
    },
    {
        "name": "{topic} metrics and dashboard",
        "purpose": "Make the state of {topic} visible, so problems surface "
        "before someone notices them by accident.",
        "tasks": [
            "Choose three numbers that show whether {topic} is healthy",
            "Find or build the source for each number",
            "Put them somewhere people already look",
        ],
    },
    {
        "name": "{topic} migration or upgrade plan",
        "purpose": "Plan a move for {topic} to a better footing, with the "
        "risky steps identified in advance.",
        "tasks": [
            "Write down the current and target states",
            "Order the steps so each one is separately reversible",
            "Identify what to do if a step has to be rolled back",
        ],
    },
)


def generate_offline(topic: str, count: int) -> IdeaSet:
    """Combine the topic with project archetypes. Deterministic by design."""
    display = topic if topic.isupper() else topic[0].upper() + topic[1:]
    lowered = topic[0].lower() + topic[1:] if topic else topic

    ideas: list[Idea] = []
    for archetype in ARCHETYPES[:count]:
        ideas.append(
            Idea(
                name=archetype["name"].format(topic=display),
                purpose=archetype["purpose"].format(topic=lowered),
                tasks=[t.format(topic=lowered) for t in archetype["tasks"]],
            )
        )
    return IdeaSet(
        topic=topic,
        ideas=ideas,
        source="offline",
        note=(
            "Generated offline from project archetypes. Set ANTHROPIC_API_KEY "
            "for ideas written for this topic specifically."
        ),
    )


# --- Claude generator -------------------------------------------------------

SYSTEM_PROMPT = """You suggest project ideas someone could actually start this week.

Each idea needs a specific name, a purpose explaining why the project is worth \
doing, and three to five concrete first tasks. Ground every idea in the topic \
you are given: a reader should not be able to swap in a different topic and \
have the idea still read the same way.

Prefer ideas that differ from each other in kind, not just in wording. Skip \
ideas that are too large to start on, and skip restatements of the topic."""


def generate_with_claude(topic: str, count: int) -> IdeaSet:
    """Ask Claude for ideas. Raises IdeaError if the call cannot be made."""
    try:
        import anthropic
    except ImportError:
        raise IdeaError("the anthropic package is not installed")

    from pydantic import BaseModel, Field

    class IdeaModel(BaseModel):
        name: str = Field(description="Short, specific project name")
        purpose: str = Field(description="One or two sentences on why it is worth doing")
        tasks: list[str] = Field(description="Three to five concrete first tasks")

    class IdeaList(BaseModel):
        ideas: list[IdeaModel]

    client = anthropic.Anthropic()
    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Suggest {count} project ideas about: {topic}\n\n"
                        f"Return exactly {count} ideas."
                    ),
                }
            ],
            output_format=IdeaList,
        )
    except anthropic.APIStatusError as exc:
        raise IdeaError(f"Claude request failed ({exc.status_code})")
    except anthropic.APIConnectionError:
        raise IdeaError("could not reach the Claude API")

    if response.stop_reason == "refusal":
        raise IdeaError("Claude declined to answer for this topic")

    parsed = response.parsed_output
    if parsed is None or not parsed.ideas:
        raise IdeaError("Claude returned no ideas")

    ideas = [
        Idea(name=i.name.strip(), purpose=i.purpose.strip(), tasks=[t.strip() for t in i.tasks])
        for i in parsed.ideas[:count]
    ]
    return IdeaSet(topic=topic, ideas=ideas, source="claude")


# --- entry point ------------------------------------------------------------


def generate_ideas(topic: str, count: int | str | None = None) -> IdeaSet:
    """Suggest projects for a topic, falling back offline when Claude is out."""
    topic = clean_topic(topic)
    wanted = clamp_count(count)

    if not api_key_available():
        return generate_offline(topic, wanted)

    try:
        return generate_with_claude(topic, wanted)
    except IdeaError as exc:
        fallback = generate_offline(topic, wanted)
        fallback.note = f"Claude was unavailable ({exc}); showing offline ideas."
        return fallback
