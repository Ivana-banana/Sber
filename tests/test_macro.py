"""What the macro controller must refuse, keep and never claim.

Every test here runs against `StubLlm`, so the suite costs no tokens and no network --
the same rule the loop's tests follow, and for the same reason: total tokens is the
second ranking criterion, so a test that reaches the endpoint spends the thing we are
trying to win.

The programs below are real and really run. That is deliberate: the whole hypothesis
under test is that a generated program does the work, so a suite that mocked execution
would be checking the plumbing and not the claim.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent import limits, macro, paths, verify
from agent.playbooks import GENERAL
from agent.ports import LlmError, LlmReply
from agent.preflight import inspect_environment
from agent.state import AgentState, Budget
from tests.stub_llm import StubLlm


def reply(program: str, deliverable: str, summary: str = "done") -> LlmReply:
    return LlmReply(
        text=json.dumps({"program": program, "deliverable": deliverable, "summary": summary}),
        input_tokens=10,
        output_tokens=5,
    )


def make_state(workspace: Path, instruction: str, named: tuple[Path, ...] = ()) -> AgentState:
    return AgentState(
        instruction=instruction,
        workspace=workspace,
        named_paths=named,
        baseline=paths.snapshot(named),
        budget=Budget(total_s=limits.MACRO_TOTAL_S),
    )


def drive(state: AgentState, llm: StubLlm) -> macro.Outcome:
    brief = inspect_environment(state.workspace, {})
    return macro.run(state, llm, verify.verify, brief=brief, playbook=GENERAL)


def test_the_macro_controller_counts_the_tokens_of_a_failed_call(tmp_path: Path) -> None:
    # A failed macro call was still generated and billed; its tokens must reach the
    # budget, the same way the main loop counts an LlmError. Regression for the
    # early-return-before-add_usage bug on the macro error path.
    target = tmp_path / "answer.txt"
    state = make_state(tmp_path, f"write {target}", (target,))
    llm = StubLlm(replies=[LlmError("endpoint down", retryable=False, input_tokens=100, output_tokens=2048)])

    drive(state, llm)

    assert state.budget.total_tokens == 2148


def test_a_program_that_writes_the_deliverable_is_solved_in_one_call(tmp_path: Path) -> None:
    target = tmp_path / "answer.txt"
    llm = StubLlm(replies=[reply("open('answer.txt','w').write('42')", str(target))])

    outcome = drive(make_state(tmp_path, f"write the answer to {target}", (target,)), llm)

    assert outcome.solved is True
    assert outcome.stop_reason == "verified"
    assert outcome.deliverable == target
    assert target.read_text() == "42"
    assert len(llm.calls) == 1


def test_a_missing_deliverable_is_not_solved_however_the_program_exited(tmp_path: Path) -> None:
    """The model's claim is not evidence. This program exits 0 and writes nothing."""
    target = tmp_path / "answer.txt"
    llm = StubLlm(replies=[reply("print('solved the task')", str(target)), LlmError("no", False)])

    outcome = drive(make_state(tmp_path, f"write {target}", (target,)), llm)

    assert outcome.solved is False
    assert outcome.verdict is not None and outcome.verdict.stage == "existence"


def test_an_unchanged_input_file_is_refused_as_the_answer(tmp_path: Path) -> None:
    """Pointing at what was already there is the cheapest fake solve there is."""
    target = tmp_path / "input.txt"
    target.write_text("original", encoding="utf-8")
    llm = StubLlm(replies=[reply("pass", str(target)), LlmError("no", False)])

    outcome = drive(make_state(tmp_path, f"fix {target}", (target,)), llm)

    assert outcome.solved is False
    assert outcome.verdict is not None and outcome.verdict.stage == "stale"


def test_a_python_deliverable_that_does_not_compile_is_never_green(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("x = 1\n", encoding="utf-8")
    llm = StubLlm(
        replies=[reply("open('module.py','w').write('def broken(:\\n')", str(target)), LlmError("no", False)]
    )

    outcome = drive(make_state(tmp_path, f"fix {target}", (target,)), llm)

    assert outcome.solved is False
    assert outcome.verdict is not None and outcome.verdict.stage == "compile"


def test_a_valid_python_deliverable_with_a_source_encoding_compiles(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_bytes(b"# coding: latin-1\nlabel = '\xe9'\n")

    assert macro._uncompilable(target) == ""


def test_touching_a_test_file_fails_the_run_even_when_the_deliverable_is_right(
    tmp_path: Path,
) -> None:
    """Weakening the check is a defect, not a solution, so it loses to nothing else."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_it.py").write_text("assert False\n", encoding="utf-8")
    target = tmp_path / "fix.py"
    program = (
        "open('fix.py','w').write('ok = 1\\n')\n"
        "open('tests/test_it.py','w').write('assert True\\n')\n"
    )
    llm = StubLlm(replies=[reply(program, str(target)), LlmError("no", False)])

    outcome = drive(make_state(tmp_path, f"fix {target}", (target,)), llm)

    assert outcome.solved is False
    assert outcome.verdict is not None and outcome.verdict.stage == "protected"


def test_a_program_that_kills_processes_is_refused_before_it_runs(tmp_path: Path) -> None:
    target = tmp_path / "answer.txt"
    program = "import os, subprocess\nsubprocess.run(['pkill', '-f', 'server'])\n"
    llm = StubLlm(replies=[reply(program, str(target)), LlmError("no", False)])

    outcome = drive(make_state(tmp_path, f"write {target}", (target,)), llm)

    assert outcome.solved is False
    assert outcome.verdict is not None and outcome.verdict.stage == "refused"
    assert not target.exists()


def test_a_deliverable_outside_the_write_policy_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere.txt"
    llm = StubLlm(replies=[reply("pass", str(outside)), LlmError("no", False)])

    outcome = drive(make_state(tmp_path, "write something", ()), llm)

    assert outcome.solved is False
    assert outcome.verdict is not None and outcome.verdict.stage == "refused"


def test_the_generated_program_never_sees_the_endpoint_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A program that dumps its environment must not be able to leak the key."""
    monkeypatch.setenv(limits.API_KEY_ENV_NAME, "sk-canary-value-1234")
    monkeypatch.setenv(limits.BASE_URL_ENV_NAME, "http://endpoint.invalid/v1")
    target = tmp_path / "env.txt"
    program = "import os, json\nopen('env.txt','w').write(json.dumps(dict(os.environ)))\n"
    llm = StubLlm(replies=[reply(program, str(target))])

    outcome = drive(make_state(tmp_path, f"write {target}", (target,)), llm)

    assert outcome.solved is True
    dumped = json.loads(target.read_text())
    assert limits.API_KEY_ENV_NAME not in dumped
    assert limits.BASE_URL_ENV_NAME not in dumped
    assert "sk-canary-value-1234" not in target.read_text()


def test_a_repair_call_follows_a_red_check_and_its_success_is_kept(tmp_path: Path) -> None:
    target = tmp_path / "answer.txt"
    llm = StubLlm(
        replies=[
            reply("pass", str(target)),
            reply("open('answer.txt','w').write('42')", str(target)),
        ]
    )

    outcome = drive(make_state(tmp_path, f"write {target}", (target,)), llm)

    assert outcome.solved is True
    assert len(llm.calls) == 2
    assert "did not solve" in llm.calls[1][-1]["content"]


def test_the_run_costs_at_most_two_logical_calls(tmp_path: Path) -> None:
    """The token bill is half the reason this controller exists; the cap is code."""
    target = tmp_path / "answer.txt"
    llm = StubLlm(replies=[reply("pass", str(target)), reply("pass", str(target)), reply("pass", str(target))])

    outcome = drive(make_state(tmp_path, f"write {target}", (target,)), llm)

    assert outcome.solved is False
    assert len(llm.calls) == limits.MACRO_MAX_LLM_CALLS


def test_a_malformed_first_reply_ends_the_run_without_a_second_chance_at_the_program(
    tmp_path: Path,
) -> None:
    llm = StubLlm(replies=[LlmReply(text="I will now solve the task.")])

    outcome = drive(make_state(tmp_path, "do the thing", ()), llm)

    assert outcome.solved is False
    assert outcome.stop_reason == "llm-unusable"
    assert len(llm.calls) == 1


def test_a_program_that_hangs_is_killed_and_reported(tmp_path: Path) -> None:
    target = tmp_path / "answer.txt"
    llm = StubLlm(
        replies=[reply("import time\ntime.sleep(60)\n", str(target)), LlmError("no", False)]
    )

    outcome = drive(make_state(tmp_path, f"write {target}", (target,)), llm)

    assert outcome.solved is False
    assert any("program" in text for text in outcome.observations)


def test_the_packet_bounds_orientation_and_preserves_the_entire_task(tmp_path: Path) -> None:
    """The task text is untrusted data and must survive; the excerpts may not."""
    big = tmp_path / "notes.txt"
    big.write_text("x" * 500, encoding="utf-8")
    instruction = "  FIRST LINE\n" + "y" * (limits.MACRO_PACKET_CHARS + 200) + "\nEND  \n"
    state = make_state(tmp_path, instruction, (big,))

    packet = macro._packet(state, inspect_environment(tmp_path, {}), GENERAL)

    task = packet.partition("TASK\n")[2]
    assert task == instruction
    assert len(packet) <= limits.MACRO_PACKET_CHARS + len(instruction)


def test_the_packet_drops_large_excerpts_before_orientation(tmp_path: Path) -> None:
    big = tmp_path / "notes.txt"
    big.write_text("excerpt marker " * 200, encoding="utf-8")
    instruction = "keep this complete task"
    state = make_state(tmp_path, instruction, (big,))

    packet = macro._packet(state, inspect_environment(tmp_path, {}), GENERAL)

    assert len(packet) <= limits.MACRO_PACKET_CHARS + len(instruction)
    assert packet.endswith("TASK\n" + instruction)


def test_a_fenced_json_reply_still_parses(tmp_path: Path) -> None:
    """The text rungs of the ladder carry no schema, and this is what comes back."""
    payload = json.dumps({"program": "pass", "deliverable": "/tmp/x"})
    solver, error = macro._parse(f"```json\n{payload}\n```")

    assert error == ""
    assert solver is not None and solver.deliverable == "/tmp/x"


def test_no_solver_program_leaves_the_workspace_behind(tmp_path: Path) -> None:
    """A stray `solver.py` beside the deliverable is an unexplained file in the answer."""
    target = tmp_path / "answer.txt"
    llm = StubLlm(replies=[reply("open('answer.txt','w').write('42')", str(target))])

    drive(make_state(tmp_path, f"write {target}", (target,)), llm)

    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["answer.txt"]
    assert os.path.exists(target)


def test_the_call_ceiling_is_derived_from_the_deadline_and_not_a_constant(
    tmp_path: Path,
) -> None:
    """E041 died on a fixed 45 s cap against a call that measures 57-74 s here.

    The ceiling has to be everything left except the seconds the generated program will
    itself need, so that a slow endpoint costs the program's refinement and never the
    reply that would have produced one.
    """
    target = tmp_path / "answer.txt"
    llm = StubLlm(replies=[reply("open('answer.txt','w').write('42')", str(target))])
    state = make_state(tmp_path, f"write {target}", (target,))

    drive(state, llm)

    asked = llm.timeouts[0]
    expected = (
        limits.MACRO_TOTAL_S - limits.MACRO_FINALIZE_RESERVE_S - limits.MACRO_PROGRAM_TIMEOUT_S
    )
    assert asked == pytest.approx(expected, abs=2.0)
    assert asked > 50.0


def test_the_first_call_is_given_more_time_than_a_program_call_measures(
    tmp_path: Path,
) -> None:
    """E043 hit its 73 s ceiling on every task with no HTTP reply at all.

    The controller has nothing on disk until its program runs, so a ceiling below what
    a call costs converts a task that might have been solved into a certain zero, and
    buys nothing in exchange -- being killed by the runner scores the same.
    """
    target = tmp_path / "answer.txt"
    llm = StubLlm(replies=[reply("open('answer.txt','w').write('42')", str(target))])
    state = AgentState(
        instruction=f"write {target}",
        workspace=tmp_path,
        named_paths=(target,),
        baseline=paths.snapshot((target,)),
        budget=Budget(total_s=limits.DEFAULT_TASK_TIMEOUT_S),
    )

    drive(state, llm)

    assert llm.timeouts[0] > 80.0
