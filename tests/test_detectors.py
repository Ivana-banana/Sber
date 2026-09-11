"""Стоп-условия петли: они обязаны срабатывать без участия модели.

Каждый тест описывает один класс нуля из `verdict-loop-failure-detectors.md`: агент,
который повторяет один и тот же неудачный вызов, ходит по кругу A↔B или крутится, не
производя ничего нового. Пороги здесь — стартовые значения из `agent/limits.py`, а не
измерения; тесты закрепляют поведение, а не доказывают, что порог верный.
"""

from __future__ import annotations

from pathlib import Path

from agent import detectors, limits
from agent.state import AgentState, Budget


def make_state(tmp_path: Path) -> AgentState:
    return AgentState(
        instruction="fix the injection",
        workspace=tmp_path,
        budget=Budget(total_s=600.0, clock=lambda: 0.0),
    )


def observe(
    state: AgentState,
    tool: str,
    args: dict[str, object],
    output: str,
    *,
    ok: bool = True,
    error_code: str = "",
) -> None:
    state.budget.start_step()
    state.observe(tool, args, output, ok=ok, error_code=error_code)


def test_canonical_args_ignore_key_order_but_not_values() -> None:
    same = detectors.args_digest({"path": "/app/main.py", "start_line": 1})
    reordered = detectors.args_digest({"start_line": 1, "path": "/app/main.py"})
    other = detectors.args_digest({"path": "/app/other.py", "start_line": 1})

    assert same == reordered, "argument order is not a difference in the action"
    assert same != other


def test_a_third_identical_failure_is_blocked_before_it_runs(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    args = {"command": "pytest /tests"}

    for _ in range(limits.REPEAT_FAILURE_BLOCK):
        observe(state, "run_shell", args, "no such file", ok=False, error_code="exit:127")

    signal = detectors.blocked(state, "run_shell", detectors.args_digest(args))

    assert signal is not None
    assert signal.kind == "block"
    assert "run_shell" in signal.detail


def test_the_guard_does_not_block_a_call_that_merely_looks_similar(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    for _ in range(limits.REPEAT_FAILURE_BLOCK):
        observe(state, "run_shell", {"command": "a"}, "boom", ok=False, error_code="exit:1")

    assert detectors.blocked(state, "run_shell", detectors.args_digest({"command": "b"})) is None
    assert detectors.blocked(state, "read_range", detectors.args_digest({"command": "a"})) is None


def test_a_succeeding_call_is_never_blocked_however_often_it_repeats(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    args = {"path": "/app"}

    for index in range(limits.REPEAT_FAILURE_BLOCK + 2):
        observe(state, "list_dir", args, f"listing {index}")

    assert detectors.blocked(state, "list_dir", detectors.args_digest(args)) is None


def test_a_repeat_whose_result_is_already_known_is_refused(tmp_path: Path) -> None:
    """P2d: 11 of 15 steps were exact duplicates whose output was already in history."""
    state = make_state(tmp_path)
    args = {"path": "/app"}

    for _ in range(limits.REPEAT_RESULT_BLOCK):
        observe(state, "list_dir", args, "same listing")

    signal = detectors.blocked(state, "list_dir", detectors.args_digest(args))

    assert signal is not None
    assert signal.kind == "block"
    assert signal.name == "repeat-result"
    assert "list_dir" in signal.detail


def test_a_repeat_from_before_state_change_is_not_blocked(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    args = {"path": "/app/source.py"}

    for _ in range(limits.REPEAT_RESULT_BLOCK):
        observe(state, "read_range", args, "old contents")

    state.advance_state_epoch()

    assert detectors.blocked(state, "read_range", detectors.args_digest(args)) is None


def test_stuck_detector_does_not_mix_epochs(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    for index in range(limits.NO_PROGRESS_STOP):
        observe(state, "read_range", {"path": "/app/source.py", "start_line": index}, "old")

    state.advance_state_epoch()

    assert detectors.stuck(state) is None


def test_the_same_action_with_the_same_result_stops_the_loop(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    for _ in range(limits.EXACT_REPEAT_STOP):
        observe(state, "list_dir", {"path": "/app"}, "same listing")

    signal = detectors.stuck(state)

    assert signal is not None
    assert signal.kind == "stuck"
    assert signal.name == "exact-repeat"


def test_two_actions_alternating_forever_stop_the_loop(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    for index in range(limits.ALTERNATING_STOP):
        if index % 2 == 0:
            observe(state, "list_dir", {"path": "/app"}, "listing")
        else:
            observe(state, "read_range", {"path": "/app/x"}, "contents")

    signal = detectors.stuck(state)

    assert signal is not None
    assert signal.name in {"alternating", "no-progress"}


def test_new_output_every_time_is_progress_and_never_stops(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    for index in range(limits.ALTERNATING_STOP + 2):
        observe(state, "run_shell", {"command": f"echo {index}"}, f"line {index}")

    assert detectors.stuck(state) is None


def test_a_window_of_already_seen_results_stops_the_loop(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    # Four distinct actions, each replaying an observation the run has already made.
    observe(state, "run_shell", {"command": "seed"}, "known")
    for index in range(limits.NO_PROGRESS_STOP):
        observe(state, "run_shell", {"command": f"variant {index}"}, "known")

    signal = detectors.stuck(state)

    assert signal is not None
    assert signal.name == "no-progress"


def test_a_written_artifact_counts_as_progress(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    observe(state, "run_shell", {"command": "seed"}, "known")
    for index in range(limits.NO_PROGRESS_STOP - 1):
        observe(state, "run_shell", {"command": f"variant {index}"}, "known")

    state.artifacts.append(tmp_path / "report.json")
    observe(state, "write_file", {"path": "report.json"}, "known")

    assert detectors.stuck(state) is None, "producing a deliverable is progress by itself"


def test_history_is_bounded_and_keeps_the_most_recent_actions(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    for index in range(limits.ACTION_HISTORY + 4):
        observe(state, "run_shell", {"command": f"echo {index}"}, f"out {index}")

    assert len(state.actions) == limits.ACTION_HISTORY
    assert state.actions[-1].tool == "run_shell"
    assert state.actions[0].step == 5, "the oldest kept action is the newest minus the window"


def test_the_fingerprint_is_taken_from_redacted_output(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.secrets = ("sk-canary-value",)

    observe(state, "run_shell", {"command": "env"}, "OPENAI_API_KEY=sk-canary-value")

    assert "sk-canary" not in state.actions[-1].result_fingerprint
    plain = detectors.digest("OPENAI_API_KEY=sk-canary-value")
    assert state.actions[-1].result_fingerprint != plain, "the secret must not reach the hash"
