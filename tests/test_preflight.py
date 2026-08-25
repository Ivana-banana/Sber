"""The map the agent gets for free, before it spends a single token.

Preflight exists because the alternative is paying a small model to discover, one
tool call at a time, facts that `os.scandir` answers instantly and correctly. Its
whole contract is: deterministic, no LLM, bounded, and never a crash on a workspace
that does not look the way we expected.
"""

from __future__ import annotations

from pathlib import Path

from agent import limits
from agent.preflight import inspect_environment

ENV = {
    "LOCAL_AGENT_MODEL": "small-local",
    "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
    "OPENAI_API_KEY": "sk-not-a-real-key-0123456789",
}


def make_task(root: Path) -> Path:
    workspace = root / "app"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "README.md").write_text("# task\n", encoding="utf-8")
    (workspace / "src" / "login.py").write_text("import sqlite3\n", encoding="utf-8")
    (workspace / "tests" / "test_login.py").write_text("def test_x(): pass\n", encoding="utf-8")
    return workspace


def test_the_brief_names_the_workspace_and_its_entries(tmp_path: Path) -> None:
    workspace = make_task(tmp_path)

    brief = inspect_environment(workspace, ENV)

    assert str(workspace) in brief.render()
    assert "README.md" in brief.render()
    assert "src/" in brief.render()


def test_the_brief_reports_which_interpreters_and_utilities_exist(tmp_path: Path) -> None:
    workspace = make_task(tmp_path)

    brief = inspect_environment(workspace, ENV)

    assert "python3" in brief.render(), "the one dependency every playbook assumes"
    assert brief.available, "at least the running interpreter must be discoverable"


def test_the_brief_stays_small_enough_to_pay_for_on_every_call(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    for i in range(400):
        (workspace / f"evidence-{i:04d}.log").write_text("x", encoding="utf-8")

    brief = inspect_environment(workspace, ENV)
    rendered = brief.render()

    assert len(rendered) <= limits.BRIEF_MAX_CHARS
    assert "more" in rendered, "a cut list must say it was cut"


def test_the_brief_never_carries_the_endpoint_key(tmp_path: Path) -> None:
    workspace = make_task(tmp_path)
    (workspace / "leak.txt").write_text(ENV["OPENAI_API_KEY"], encoding="utf-8")

    rendered = inspect_environment(workspace, ENV).render()

    assert ENV["OPENAI_API_KEY"] not in rendered


def test_a_workspace_that_does_not_exist_still_produces_a_brief(tmp_path: Path) -> None:
    brief = inspect_environment(tmp_path / "gone", ENV)

    assert brief.render()
    assert "missing" in brief.render()


def test_a_workspace_we_may_not_read_is_reported_not_raised(tmp_path: Path, monkeypatch) -> None:
    workspace = make_task(tmp_path)

    def deny(*args: object, **kwargs: object) -> object:
        raise PermissionError("denied")

    monkeypatch.setattr("agent.preflight.os.scandir", deny)

    brief = inspect_environment(workspace, ENV)

    assert "unreadable" in brief.render()


def test_nested_directories_are_summarised_but_not_walked_forever(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    deep = workspace
    for level in range(12):
        deep = deep / f"level{level}"
    deep.mkdir(parents=True)
    (deep / "buried.txt").write_text("x", encoding="utf-8")

    rendered = inspect_environment(workspace, ENV).render()

    assert "level0/" in rendered
    assert "level9" not in rendered, "depth is capped; the loop searches the rest on demand"
