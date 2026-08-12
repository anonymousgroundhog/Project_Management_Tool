"""Project idea generation, the offline fallback, and accepting an idea.

The Claude path is exercised with a stubbed client so the suite never needs an
API key or a network call.
"""

import importlib
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Default every test to the offline generator unless it opts out."""
    monkeypatch.setenv("PMTOOL_OFFLINE_IDEAS", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PMTOOL_DB", str(tmp_path / "ideas.db"))
    for name in ("app.main", "app.db", "app.models", "app.services", "app.cli", "app.ideas"):
        sys.modules.pop(name, None)
    main = importlib.import_module("app.main")
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PMTOOL_DB", str(tmp_path / "ideas_cli.db"))
    for name in ("app.main", "app.db", "app.models", "app.services", "app.cli", "app.ideas"):
        sys.modules.pop(name, None)
    cli = importlib.import_module("app.cli")

    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


@pytest.fixture()
def ideas():
    sys.modules.pop("app.ideas", None)
    return importlib.import_module("app.ideas")


# --- offline generator ------------------------------------------------------


def test_offline_ideas_mention_the_topic(ideas):
    result = ideas.generate_ideas("home automation", 5)
    assert result.source == "offline"
    assert len(result.ideas) == 5
    for idea in result.ideas:
        assert "home automation" in (idea.name + idea.purpose).lower()
        assert idea.purpose
        assert len(idea.tasks) >= 3


def test_offline_ideas_are_distinct(ideas):
    result = ideas.generate_ideas("home automation", 8)
    names = [i.name for i in result.ideas]
    assert len(set(names)) == len(names)


def test_count_is_clamped_and_defaulted(ideas):
    assert len(ideas.generate_ideas("robots").ideas) == ideas.DEFAULT_IDEAS
    assert len(ideas.generate_ideas("robots", 1).ideas) == 1
    assert len(ideas.generate_ideas("robots", 999).ideas) == ideas.MAX_IDEAS
    assert len(ideas.generate_ideas("robots", 0).ideas) == 1


def test_bad_count_rejected(ideas):
    with pytest.raises(ideas.IdeaError):
        ideas.generate_ideas("robots", "soon")


def test_empty_topic_rejected(ideas):
    for bad in ("", "   ", None):
        with pytest.raises(ideas.IdeaError):
            ideas.generate_ideas(bad)


def test_topic_is_normalized_and_capped(ideas):
    result = ideas.generate_ideas("  home   automation  ", 1)
    assert result.topic == "home automation"
    assert ideas.generate_ideas("x" * 500, 1).topic == "x" * 120


def test_acronym_topic_keeps_its_case(ideas):
    result = ideas.generate_ideas("CI", 1)
    assert "CI" in result.ideas[0].name


# --- Claude path (stubbed) --------------------------------------------------


def install_fake_anthropic(monkeypatch, *, parsed=None, raises=None, stop_reason="end_turn"):
    """Install a stand-in `anthropic` module so no network call is made.

    `raises` names which failure `messages.parse` should raise — "status" or
    "connection" — so the exception class always comes from the same module
    object the code under test catches against.
    """

    class APIStatusError(Exception):
        def __init__(self, status_code=500):
            super().__init__(f"status {status_code}")
            self.status_code = status_code

    class APIConnectionError(Exception):
        pass

    class FakeMessages:
        def parse(self, **kwargs):
            self.kwargs = kwargs
            if raises == "status":
                raise APIStatusError(500)
            if raises == "connection":
                raise APIConnectionError()
            return types.SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed)

    class FakeAnthropic:
        def __init__(self, *a, **kw):
            self.messages = FakeMessages()
            FakeAnthropic.last = self

    module = types.ModuleType("anthropic")
    module.Anthropic = FakeAnthropic
    module.APIStatusError = APIStatusError
    module.APIConnectionError = APIConnectionError
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return module


def fake_parsed(count=2):
    from pydantic import BaseModel

    class I(BaseModel):
        name: str
        purpose: str
        tasks: list[str]

    class L(BaseModel):
        ideas: list[I]

    return L(
        ideas=[
            I(
                name=f"Claude idea {n}",
                purpose=f"Because reason {n}",
                tasks=[f"task {n}a", f"task {n}b", f"task {n}c"],
            )
            for n in range(1, count + 1)
        ]
    )


def test_claude_path_used_when_key_present(ideas, monkeypatch):
    monkeypatch.delenv("PMTOOL_OFFLINE_IDEAS", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    module = install_fake_anthropic(monkeypatch, parsed=fake_parsed(2))

    result = ideas.generate_ideas("home automation", 2)
    assert result.source == "claude"
    assert [i.name for i in result.ideas] == ["Claude idea 1", "Claude idea 2"]

    sent = module.Anthropic.last.messages.kwargs
    assert sent["model"] == "claude-opus-5"
    assert "home automation" in sent["messages"][0]["content"]


def test_claude_connection_failure_falls_back_offline(ideas, monkeypatch):
    monkeypatch.delenv("PMTOOL_OFFLINE_IDEAS", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    install_fake_anthropic(monkeypatch, raises="connection")

    result = ideas.generate_ideas("home automation", 3)
    assert result.source == "offline"
    assert len(result.ideas) == 3
    assert "could not reach" in result.note


def test_claude_refusal_falls_back_offline(ideas, monkeypatch):
    monkeypatch.delenv("PMTOOL_OFFLINE_IDEAS", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    install_fake_anthropic(monkeypatch, parsed=fake_parsed(2), stop_reason="refusal")

    result = ideas.generate_ideas("home automation", 2)
    assert result.source == "offline"
    assert "declined" in result.note


def test_api_error_falls_back_offline(ideas, monkeypatch):
    monkeypatch.delenv("PMTOOL_OFFLINE_IDEAS", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    install_fake_anthropic(monkeypatch, raises="status")

    result = ideas.generate_ideas("home automation", 2)
    assert result.source == "offline"
    assert "unavailable" in result.note
    assert "500" in result.note


def test_offline_env_var_wins_over_key(ideas, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("PMTOOL_OFFLINE_IDEAS", "1")
    assert ideas.generate_ideas("robots", 2).source == "offline"


# --- web --------------------------------------------------------------------


def test_ideas_page_renders_form(client):
    page = client.get("/ideas")
    assert page.status_code == 200
    assert "Project ideas" in page.text
    assert "Suggest projects" in page.text


def test_ideas_page_lists_suggestions(client):
    page = client.get("/ideas?topic=home+automation&count=3")
    assert page.status_code == 200
    assert "Home automation" in page.text
    assert page.text.count("Start this project") == 3


def test_ideas_htmx_returns_fragment(client):
    resp = client.get("/ideas?topic=robots", headers={"HX-Request": "true"})
    assert "<html" not in resp.text
    assert 'id="ideas"' in resp.text


def test_bad_count_shows_error_not_crash(client):
    page = client.get("/ideas?topic=robots&count=soon")
    assert page.status_code == 200
    assert "count must be a number" in page.text


def test_accepting_an_idea_creates_project_with_tasks(client):
    resp = client.post(
        "/ideas/accept",
        data={
            "name": "Home automation audit",
            "purpose": "Know what exists first",
            "tasks": ["List the devices", "Note what breaks"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    detail = client.get(resp.headers["location"])
    assert "Home automation audit" in detail.text
    assert "Know what exists first" in detail.text
    assert "List the devices" in detail.text
    assert "Note what breaks" in detail.text


def test_accepting_an_idea_without_tasks(client):
    resp = client.post(
        "/ideas/accept",
        data={"name": "Bare idea", "purpose": "p"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    detail = client.get(resp.headers["location"])
    assert "Bare idea" in detail.text
    assert "0% done · 0 open" in detail.text


def test_accepted_idea_appears_in_project_list(client):
    client.post(
        "/ideas/accept",
        data={"name": "From an idea", "purpose": "p", "tasks": ["first task"]},
    )
    assert "From an idea" in client.get("/").text


def test_accept_requires_a_name(client):
    assert client.post("/ideas/accept", data={"name": "  "}).status_code == 400


def test_empty_project_list_links_to_ideas(client):
    assert "/ideas" in client.get("/").text


# --- CLI --------------------------------------------------------------------


def test_cli_lists_ideas(run):
    code, out, _ = run("ideas", "home automation", "--count", "3")
    assert code == 0
    assert "3 idea(s) for 'home automation'" in out
    assert out.count("Purpose:") == 3


def test_cli_accepts_an_idea(run):
    code, out, _ = run("ideas", "home automation", "--count", "3", "--accept", "2")
    assert code == 0
    assert "Created project 1" in out

    _, out, _ = run("show", "1")
    assert "prototype" in out.lower()
    assert out.count("[ ]") >= 3


def test_cli_rejects_out_of_range_accept(run):
    code, _, err = run("ideas", "robots", "--count", "2", "--accept", "9")
    assert code == 1
    assert "--accept must be between 1 and 2" in err


def test_cli_rejects_empty_topic(run):
    code, _, err = run("ideas", "   ")
    assert code == 1
    assert "topic required" in err
