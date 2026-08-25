"""External verification: what decides `ok`, per `docs/research/verifier-contract.md`.

Every one of the six public verifiers reads the environment, never agent stdout, and
is binary on exit code / file state rather than on wording. These tests hold `verify()`
to the same rule: existence and content decide, a command's exit code decides, and an
empty verify command is a deliberate `unverified` pass, not a failure (M13: the real
verifier reads the environment regardless of what we check here).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.state import AgentState, Budget
from agent.verify import verify

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="target image is Linux; /bin/sh absent on Windows")


def make_state(tmp_path: Path, *, total_s: float = 600.0, secrets: tuple[str, ...] = ()) -> AgentState:
    state = AgentState(
        instruction="produce the deliverable",
        workspace=tmp_path,
        budget=Budget(total_s=total_s, clock=lambda: 0.0),
    )
    state.secrets = secrets
    return state


def test_a_workspace_relative_deliverable_resolves_against_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "bye.txt").write_text("Bye", encoding="utf-8")
    state = make_state(tmp_path)

    result = verify(state, deliverable=Path("bye.txt"), command="")

    assert result.ok is True, "finish() gives a model-chosen path, which may be relative"
    assert result.stage == "unverified"


def test_a_deliverable_path_escaping_the_workspace_fails_at_existence(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("Bye", encoding="utf-8")
    state = make_state(workspace)

    result = verify(state, deliverable=Path("..") / "outside.txt", command="")

    assert result.ok is False
    assert result.stage == "existence"


def test_a_missing_deliverable_fails_at_existence(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    result = verify(state, deliverable=tmp_path / "report.json", command="")

    assert result.ok is False
    assert result.stage == "existence"
    assert "report.json" in result.detail


def test_a_directory_in_place_of_the_deliverable_fails_at_existence(tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    target.mkdir()
    state = make_state(tmp_path)

    result = verify(state, deliverable=target, command="")

    assert result.ok is False
    assert result.stage == "existence"


def test_an_empty_deliverable_fails_at_non_empty(tmp_path: Path) -> None:
    target = tmp_path / "bye.txt"
    target.write_text("", encoding="utf-8")
    state = make_state(tmp_path)

    result = verify(state, deliverable=target, command="")

    assert result.ok is False
    assert result.stage == "non-empty"


def test_a_present_non_empty_deliverable_with_no_command_is_unverified_but_ok(tmp_path: Path) -> None:
    target = tmp_path / "bye.txt"
    target.write_text("Bye", encoding="utf-8")
    state = make_state(tmp_path)

    result = verify(state, deliverable=target, command="   ")

    assert result.ok is True, "the real verifier reads the environment, not us (M13)"
    assert result.stage == "unverified"


@POSIX_ONLY
def test_a_verify_command_that_exits_zero_passes(tmp_path: Path) -> None:
    target = tmp_path / "bye.txt"
    target.write_text("Bye", encoding="utf-8")
    state = make_state(tmp_path)

    result = verify(state, deliverable=target, command="true")

    assert result.ok is True
    assert result.stage == "command"


@POSIX_ONLY
def test_a_verify_command_that_exits_non_zero_fails_even_if_it_prints_success(tmp_path: Path) -> None:
    target = tmp_path / "bye.txt"
    target.write_text("Bye", encoding="utf-8")
    state = make_state(tmp_path)

    result = verify(state, deliverable=target, command="echo PASSED; exit 1")

    assert result.ok is False, "exit code decides, not the words the command printed"
    assert result.stage == "command"


@POSIX_ONLY
def test_a_deliverable_that_looks_right_but_fails_its_own_command_is_not_ok(tmp_path: Path) -> None:
    target = tmp_path / "bye.txt"
    target.write_text("Bye\n", encoding="utf-8")
    state = make_state(tmp_path)

    result = verify(state, deliverable=target, command='[ "$(cat bye.txt)" = "Bye" ]')

    assert result.ok is True

    target.write_text("Bye extra\n", encoding="utf-8")
    result = verify(state, deliverable=target, command='[ "$(cat bye.txt)" = "Bye" ]')

    assert result.ok is False
    assert result.stage == "command"


@POSIX_ONLY
def test_the_command_timeout_comes_from_the_budget_not_a_tool_default(tmp_path: Path) -> None:
    target = tmp_path / "bye.txt"
    target.write_text("Bye", encoding="utf-8")
    # A near-exhausted budget leaves a sub-second tool timeout; the command sleeps
    # longer than that, so a passing test proves the budget's number was honoured.
    state = make_state(tmp_path, total_s=0.3)

    result = verify(state, deliverable=target, command="sleep 5")

    assert result.ok is False
    assert result.stage == "command"
    assert "timeout" in result.detail.lower()


@POSIX_ONLY
def test_secrets_configured_on_the_state_do_not_reach_the_detail(tmp_path: Path) -> None:
    target = tmp_path / "bye.txt"
    target.write_text("Bye", encoding="utf-8")
    state = make_state(tmp_path, secrets=("sk-canary-0123456789",))

    result = verify(state, deliverable=target, command="echo sk-canary-0123456789; exit 1")

    assert "sk-canary-0123456789" not in result.detail
