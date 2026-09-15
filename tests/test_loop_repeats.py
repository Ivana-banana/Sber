"""An action identical to the previous one, with the identical observation, is a loop
and not new evidence -- whatever the state-change classifier says about the tool.

Measured on the 4080 7B stand (F4-7b-regression-4080, 2026-09-15): nineteen byte-identical
`write_file` calls burned all thirty steps of find-sqli-login r1, and `pytest tests/`
ran eleven times in a row in fix-sqli-login r2. Every one of those calls advanced the
state epoch, so every detector started from an empty history and none ever fired.
"""

from __future__ import annotations

from pathlib import Path

from agent import limits
from tests.stub_llm import StubLlm
from tests.test_loop import FakeVerify, action, make_state, run


def test_identical_writes_do_not_reset_the_repeat_detectors(tmp_path: Path) -> None:
    target = tmp_path / "security_report.json"
    state = make_state(tmp_path)
    state.budget.max_steps = 12
    llm = StubLlm(
        replies=[action("write_file", path=str(target), content='{"findings": []}')] * 12
    )

    outcome = run(state, llm, FakeVerify((False, "command", "wrong")))

    assert outcome.stop_reason == "stuck"
    assert len(llm.calls) <= limits.REPEAT_RESULT_BLOCK + limits.REPEAT_FAILURE_BLOCK
    assert state.state_epoch == 1, "only the first write changed anything"


def test_a_repeated_shell_command_with_the_same_output_keeps_the_epoch(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.budget.max_steps = 8
    # `touch` is a state-changing command to the classifier; its output is empty every time.
    llm = StubLlm(replies=[action("run_shell", command="touch marker")] * 8)

    run(state, llm, FakeVerify())

    assert state.state_epoch == 1
    # Nothing is written, so the block stays advice and the caps do the ending; but the
    # copies are refused before they run, and the model is told why every time.
    assert len(state.actions) == limits.REPEAT_RESULT_BLOCK
    refusals = [event for event in state.audit if event.name == "repeat-result"]
    assert len(refusals) == 8 - limits.REPEAT_RESULT_BLOCK


def test_a_write_with_different_content_still_advances_the_epoch(tmp_path: Path) -> None:
    """The fix is about identical repeats only: a write that changes the file is a
    mutation, and the detectors must forget the evidence gathered before it."""
    target = tmp_path / "source.py"
    state = make_state(tmp_path)
    state.budget.max_steps = 3
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content="one"),
            action("write_file", path=str(target), content="two"),
            action("write_file", path=str(target), content="three"),
        ]
    )

    run(state, llm, FakeVerify())

    assert state.state_epoch == 3
