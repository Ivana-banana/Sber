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


def test_the_brief_names_the_utilities_the_image_does_not_have(tmp_path: Path, monkeypatch) -> None:
    """Absence is the half of the toolchain answer the model cannot look up.

    The target image has no `sqlite3` and no `xxd`, measured twice by execution. A
    forensics playbook reaches for both by reflex, and a brief that only lists what
    exists leaves the model to discover that with a failed command and a recovery
    step. Saying it once is cheaper than paying for it every run.
    """
    workspace = make_task(tmp_path)
    monkeypatch.setattr(
        "agent.preflight.shutil.which",
        lambda name: None if name in {"sqlite3", "xxd"} else f"/usr/bin/{name}",
    )

    brief = inspect_environment(workspace, ENV)
    rendered = brief.render()

    assert "sqlite3" in brief.absent and "xxd" in brief.absent
    assert "absent: " in rendered
    assert "rg" in brief.available, "the image has ripgrep; a tree-wide grep is the worse plan"
    assert "patch" in brief.available and "diff" in brief.available


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


def test_an_absent_tool_with_a_verified_substitute_names_it(tmp_path: Path, monkeypatch) -> None:
    # Reporting `sqlite3: absent` only tells the model its first plan is dead. The
    # substitutes below were run inside secureintelligent/acp on 2026-08-26, so the
    # brief can hand it the second plan in the same breath instead of paying for a
    # failed call and a recovery step.
    workspace = make_task(tmp_path)
    monkeypatch.setattr("agent.preflight.shutil.which", lambda name: None)

    rendered = inspect_environment(workspace, ENV).render()

    assert "instead:" in rendered
    assert "od -A x -t x1z" in rendered, "xxd"
    assert "tcpdump -nn -r" in rendered, "tshark"
    assert "nc -z" in rendered, "nmap"
    assert "objdump -d" in rendered, "gdb"


def test_a_tool_that_is_present_is_never_offered_a_substitute(tmp_path: Path, monkeypatch) -> None:
    # The substitution line is priced on every request. On an image where the real
    # utilities exist it must disappear entirely, not degrade to advice nobody needs.
    workspace = make_task(tmp_path)
    monkeypatch.setattr("agent.preflight.shutil.which", lambda name: f"/usr/bin/{name}")

    rendered = inspect_environment(workspace, ENV).render()

    assert "absent:" not in rendered
    assert "instead:" not in rendered


def test_only_a_measured_substitute_is_offered(tmp_path: Path, monkeypatch) -> None:
    # A tool absent with no verified replacement is reported absent and nothing more.
    # Guessing a second plan is worse than admitting there is none.
    workspace = make_task(tmp_path)
    monkeypatch.setattr(
        "agent.preflight.shutil.which", lambda name: None if name == "jq" else f"/usr/bin/{name}"
    )

    rendered = inspect_environment(workspace, ENV).render()

    assert "absent: jq" in rendered
    assert "instead:" not in rendered
