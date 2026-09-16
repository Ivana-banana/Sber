"""The trace is the observability mechanism, so it has to be trustworthy."""

from __future__ import annotations

from pathlib import Path

from agent import limits
from agent.state import AgentState, Budget


def make_state(tmp_path: Path) -> AgentState:
    return AgentState(
        instruction="find the flag",
        workspace=tmp_path,
        budget=Budget(total_s=600.0, clock=lambda: 0.0),
    )


def test_events_are_appended_in_order_and_carry_the_step(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    state.budget.start_step()
    state.record("llm", "plan", "chose to list the workspace")
    state.record("tool", "run", "ls -la", duration_s=0.02, exit_code=0)
    state.budget.start_step()
    state.record("tool", "run", "grep -r flag", exit_code=1)

    assert [(e.step, e.kind, e.name) for e in state.audit] == [
        (1, "llm", "plan"),
        (1, "tool", "run"),
        (2, "tool", "run"),
    ]


def test_a_failing_tool_keeps_its_exit_code(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    event = state.record("tool", "run", "false", exit_code=1)
    assert event.exit_code == 1, "a non-zero exit must never be flattened into success"


def test_nothing_rewrites_a_recorded_event(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.record("verify", "pytest", "2 failed")
    state.record("verify", "pytest", "0 failed")

    assert len(state.audit) == 2, "the trace is append-only, a retry does not erase the failure"
    assert state.audit[0].detail == "2 failed"


def test_a_broken_diagnostic_sink_cannot_break_the_agent(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    def broken_sink(_event) -> None:
        raise OSError("diagnostic pipe closed")

    state.event_sink = broken_sink
    event = state.record("phase", "plan", "start")

    assert state.audit == [event], "the append-only trace survives optional output failure"


def test_a_dead_diagnostic_sink_says_so_once_instead_of_going_quiet(
    tmp_path: Path, capsys
) -> None:
    """Silence and a broken sink look identical in a log; only one of them is a bug."""
    state = make_state(tmp_path)
    calls: list[str] = []

    def broken_sink(event) -> None:
        calls.append(event.name)
        raise OSError("diagnostic pipe closed")

    state.event_sink = broken_sink
    state.record("phase", "plan", "start")
    state.record("phase", "plan", "end")

    assert calls == ["plan"], "a sink that raised once is not called again"
    err = capsys.readouterr().err
    assert err.splitlines() == ["agent phase: telemetry disabled after OSError"]
    assert "diagnostic pipe closed" not in err, "the trace may not leak through an error"


def test_repeats_last_action_needs_the_same_tool_arguments_and_output(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    args = {"path": "/app/report.json", "content": "{}"}
    state.observe("write_file", args, "wrote 2 bytes")

    assert state.repeats_last_action("write_file", args, "wrote 2 bytes") is True
    assert state.repeats_last_action("write_file", {**args, "content": "{ }"}, "wrote 2 bytes") is False
    assert state.repeats_last_action("write_file", args, "wrote 3 bytes") is False
    assert state.repeats_last_action("read_range", args, "wrote 2 bytes") is False


def test_repeats_last_action_looks_only_at_the_current_epoch(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    args = {"path": "/app/report.json", "content": "{}"}
    state.observe("write_file", args, "wrote 2 bytes")
    state.advance_state_epoch()

    assert state.repeats_last_action("write_file", args, "wrote 2 bytes") is False


def test_repeats_last_action_is_false_on_an_empty_history(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assert state.repeats_last_action("write_file", {"path": "/x"}, "") is False


def test_a_note_is_stored_redacted_bounded_and_on_one_line(tmp_path: Path) -> None:
    """The scratchpad is model text we keep across calls, so it obeys the trace's rules.

    Same three rules the rest of this file guards for observations: a secret never
    survives, the size is ours to decide and not the model's, and what is kept is
    exactly what will be read back -- one line, so a note cannot forge the block it
    lives in.
    """
    state = make_state(tmp_path)
    state.secrets = ("sk-do-not-keep",)
    state.budget.start_step()

    stored = state.remember("saw sk-do-not-keep\nand a second line " + "x" * 400)

    assert "sk-do-not-keep" not in stored
    assert "\n" not in stored
    assert len(stored) == limits.NOTE_MAX_CHARS
    assert state.notes == [(1, stored)]
    assert [event.kind for event in state.audit] == ["note"]


def test_a_note_that_says_nothing_is_not_recorded(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    assert state.remember(" \n ") == ""
    assert state.notes == []
    assert state.audit == [], "an ignored note must not leave a trace entry either"
