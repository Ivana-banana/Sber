"""The trace is the observability mechanism, so it has to be trustworthy."""

from __future__ import annotations

from pathlib import Path

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
