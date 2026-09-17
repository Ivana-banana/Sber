"""Петля: единственное место, где решается, станет ли задача единицей или нулём.

Тесты описывают не «модель ответила хорошо», а то, что петля обязана делать, когда
модель ответила плохо: соврала о готовности, повторяет сломанную команду, ходит по
кругу, молчит, возвращает мусор вместо JSON или объявляет результат, которого нет.
Все они — наблюдаемые классы нулей, и ни один из них не должен зависеть от того,
что скажет о себе сама модель.

Модель здесь — `StubLlm` с записанной лентой ответов: прогон детерминирован, ничего
не стоит в токенах и не ходит в сеть. `run_shell` намеренно не используется — тесты
должны идти и на Windows, где `/bin/sh` отсутствует.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent import limits, loop, playbooks, result_progress
from agent.ports import LlmError, LlmReply
from agent.preflight import EnvironmentBrief
from agent.state import AgentState, Budget
from agent.verify import VerifyResult
from tests.stub_llm import StubLlm


@dataclass
class FakeVerify:
    """Stands in for `agent.verify.verify` under the agreed contract."""

    verdicts: list[tuple[bool, str, str]]
    calls: list[tuple[Path, str]]

    def __init__(self, *verdicts: tuple[bool, str, str]) -> None:
        self.verdicts = list(verdicts)
        self.calls = []

    def __call__(self, state: AgentState, *, deliverable: Path, command: str) -> VerifyResult:
        self.calls.append((deliverable, command))
        ok, stage, detail = self.verdicts.pop(0) if self.verdicts else (True, "existence", "")
        return VerifyResult(ok=ok, stage=stage, detail=detail)


def action(tool: str, **fields: object) -> LlmReply:
    return LlmReply(text=json.dumps({"tool": tool, **fields}), input_tokens=10, output_tokens=5)


def make_state(tmp_path: Path, *, total_s: float = 600.0, clock: object = None) -> AgentState:
    ticker = clock if clock is not None else (lambda: 0.0)
    return AgentState(
        instruction="сделай отчёт",
        workspace=tmp_path,
        budget=Budget(total_s=total_s, clock=ticker),  # type: ignore[arg-type]
    )


def make_brief(tmp_path: Path) -> EnvironmentBrief:
    return EnvironmentBrief(workspace=tmp_path, exists=True, python="3.12.13")


def run(
    state: AgentState,
    llm: StubLlm,
    verify: FakeVerify | None = None,
    *,
    playbook: playbooks.Playbook = playbooks.FORENSICS,
) -> loop.Outcome:
    return loop.run(
        state,
        llm,
        verify or FakeVerify(),
        brief=make_brief(state.workspace),
        playbook=playbook,
    )


# --- the happy path is one test; the rest are the ways it goes wrong -----------


def test_a_written_and_verified_deliverable_is_the_only_way_to_solve(tmp_path: Path) -> None:
    target = tmp_path / "incident.txt"
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content="a=b"),
            action("finish", path=str(target), verify="test -s incident.txt", summary="готово"),
        ]
    )
    verify = FakeVerify((True, "command", ""))

    outcome = run(make_state(tmp_path), llm, verify)

    assert outcome.solved is True
    assert outcome.stop_reason == "verified"
    assert outcome.deliverable == target
    assert verify.calls == [(target, "test -s incident.txt")]


def _codefix_state(tmp_path: Path, target: Path, *, max_steps: int = 2):
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    state.budget.max_steps = max_steps
    return state


def test_code_fix_refuses_a_blind_write_over_an_unread_source(tmp_path: Path) -> None:
    # The 14B fix-sqli failure: the run overwrote a router it never opened with a stub,
    # broke the import, and the verifier never ran. A write_file to an existing source
    # this run has not read is refused, with a hint, and the file is left intact.
    target = tmp_path / "routers" / "auth.py"
    target.parent.mkdir()
    target.write_text("router = APIRouter()\n\ndef login():\n    return 1\n", encoding="utf-8")
    state = _codefix_state(tmp_path, target, max_steps=1)
    llm = StubLlm(replies=[action("write_file", path=str(target), content="stub\n")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8").startswith("router ="), "the source is untouched"
    assert any("has not read it" in o for o in outcome.observations)


def test_code_fix_allows_the_write_after_the_file_was_read(tmp_path: Path) -> None:
    target = tmp_path / "routers" / "auth.py"
    target.parent.mkdir()
    target.write_text("router = 1\n", encoding="utf-8")
    state = _codefix_state(tmp_path, target, max_steps=2)
    llm = StubLlm(replies=[
        action("read_range", path=str(target), start_line=1),
        action("write_file", path=str(target), content="router = 2\n"),
    ])

    run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "router = 2\n"


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_allows_the_write_after_a_read_only_shell_opened_the_file(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("v = 1\n", encoding="utf-8")
    state = _codefix_state(tmp_path, target, max_steps=2)
    llm = StubLlm(replies=[
        action("run_shell", command=f"cat {target}"),
        action("write_file", path=str(target), content="v = 2\n"),
    ])

    run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "v = 2\n"


def test_code_fix_allows_a_write_to_a_new_file(tmp_path: Path) -> None:
    target = tmp_path / "new_module.py"
    state = _codefix_state(tmp_path, target, max_steps=1)
    llm = StubLlm(replies=[action("write_file", path=str(target), content="x = 1\n")])

    run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.exists()


def test_code_fix_allows_rewriting_a_file_the_run_created(tmp_path: Path) -> None:
    target = tmp_path / "gen.py"
    state = _codefix_state(tmp_path, target, max_steps=2)
    llm = StubLlm(replies=[
        action("write_file", path=str(target), content="a = 1\n"),
        action("write_file", path=str(target), content="a = 2\n"),
    ])

    run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "a = 2\n"


def test_code_fix_does_not_block_a_report_template_or_empty_file(tmp_path: Path) -> None:
    # .txt is not a source candidate; an empty existing file is not a broken overwrite.
    report = tmp_path / "report.txt"
    report.write_text("draft\n", encoding="utf-8")
    empty = tmp_path / "empty.py"
    empty.write_text("", encoding="utf-8")
    state = _codefix_state(tmp_path, report, max_steps=2)
    llm = StubLlm(replies=[
        action("write_file", path=str(report), content="final\n"),
        action("write_file", path=str(empty), content="x = 1\n"),
    ])

    run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert report.read_text(encoding="utf-8") == "final\n"
    assert empty.read_text(encoding="utf-8") == "x = 1\n"


def test_repeated_blind_writes_never_overwrite_the_source(tmp_path: Path) -> None:
    # However many times the model blindly retries, the unread source is never written
    # -- the catastrophic "stub broke the import" outcome cannot happen -- and the run
    # ends (repeat/no-progress detectors), not by silently succeeding.
    target = tmp_path / "svc.py"
    target.write_text("router = 1\n", encoding="utf-8")
    state = _codefix_state(tmp_path, target, max_steps=20)
    llm = StubLlm(replies=[action("write_file", path=str(target), content="stub\n")] * 20)

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "router = 1\n", "the source is never overwritten"
    assert outcome.solved is False
    # Honest about the cost: identical blind writes are refused every time and the run
    # grinds to the step cap -- a block here is advice, not a stop -- so this is 20
    # wasted calls, not a save. The guard's value is only that the file stays intact.
    assert outcome.stop_reason == "steps"
    assert sum("has not read it" in o for o in outcome.observations) >= 2


def test_f10_blind_retries_after_an_honest_edit_end_in_stuck(tmp_path: Path) -> None:
    # After one honest edit of A, blind writes to an unread B are refused; with the same
    # refusal text they read as no progress and the run ends stuck rather than at the cap.
    a = tmp_path / "a.py"
    a.write_text("x = 1\n", encoding="utf-8")
    b = tmp_path / "b.py"
    b.write_text("y = 1\n", encoding="utf-8")
    state = _codefix_state(tmp_path, a, max_steps=20)
    setup = [
        action("read_range", path=str(a), start_line=1),
        action("write_file", path=str(a), content="x = 2\n"),
    ]
    blind = [action("write_file", path=str(b), content=f"stub{i}\n") for i in range(12)]
    llm = StubLlm(replies=setup + blind)

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert b.read_text(encoding="utf-8") == "y = 1\n", "the unread second file is never overwritten"
    assert outcome.stop_reason == "stuck", outcome.stop_reason


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_allows_a_write_after_a_shell_rename_created_the_file(tmp_path: Path) -> None:
    # `mv`/`cp -p` preserve mtime but bump ctime; touched-since checks both, so a renamed
    # module is known and a later write_file to it is not a false refusal.
    src = tmp_path / "orig.py"
    src.write_text("x = 1\n", encoding="utf-8")
    dst = tmp_path / "renamed.py"
    state = _codefix_state(tmp_path, dst, max_steps=2)
    llm = StubLlm(replies=[
        action("run_shell", command=f"mv {src} {dst}"),
        action("write_file", path=str(dst), content="x = 2\n"),
    ])

    run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert dst.read_text(encoding="utf-8") == "x = 2\n"


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_allows_a_write_to_a_file_a_shell_command_created(tmp_path: Path) -> None:
    # The zeroing scenario the review found: a file created via run_shell is in neither
    # read_paths nor artifacts, so without touched-file tracking a later write_file to it
    # is falsely refused. `echo > new.py` then write_file new.py must be allowed.
    target = tmp_path / "new.py"
    state = _codefix_state(tmp_path, target, max_steps=2)
    llm = StubLlm(replies=[
        action("run_shell", command=f"echo 'x = 1' > {target}"),
        action("write_file", path=str(target), content="x = 2\n"),
    ])

    run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "x = 2\n"


def test_read_paths_from_shell_counts_the_first_pipe_segment(tmp_path: Path) -> None:
    # Unit-level: a piped read (`cat f | head`) resolves `f`. In code-fix the shell guard
    # refuses pipes before they run, so this only fires if that policy is relaxed, but
    # the resolution itself must be correct. `/bin/cat` (normalised) counts; `ls` does not.
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    state = make_state(tmp_path)
    assert loop._read_paths_from_shell(f"cat {tmp_path/'a.py'} | head -n 5", state) == [(tmp_path / "a.py").resolve()]
    assert loop._read_paths_from_shell(f"/bin/cat {tmp_path/'a.py'}", state) == [(tmp_path / "a.py").resolve()]
    assert loop._read_paths_from_shell(f"ls {tmp_path/'a.py'}", state) == [], "ls does not count as reading"


def test_code_fix_refuses_write_file_to_an_unread_json_config_but_allows_write_json(tmp_path: Path) -> None:
    # A .json config is a source candidate, so a blind write_file over an existing one is
    # refused; write_json is the deliverable tool and is never blocked.
    cfg = tmp_path / "config.json"
    cfg.write_text('{"debug": true}\n', encoding="utf-8")
    state = _codefix_state(tmp_path, cfg, max_steps=1)
    blind = StubLlm(replies=[action("write_file", path=str(cfg), content='{"debug": false}\n')])
    run(state, blind, FakeVerify(), playbook=playbooks.CODE_FIX)
    assert cfg.read_text(encoding="utf-8") == '{"debug": true}\n', "write_file over unread config is refused"

    state2 = _codefix_state(tmp_path, cfg, max_steps=1)
    viajson = StubLlm(replies=[action("write_json", path=str(cfg), data={"debug": False})])
    run(state2, viajson, FakeVerify(), playbook=playbooks.CODE_FIX)
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"debug": False}, "write_json is not blocked"


def test_code_fix_does_not_block_replace_text_on_an_unread_file(tmp_path: Path) -> None:
    target = tmp_path / "svc.py"
    target.write_text("value = 1\n", encoding="utf-8")
    state = _codefix_state(tmp_path, target, max_steps=1)
    llm = StubLlm(replies=[action("replace_text", path=str(target), old="value = 1", new="value = 2")])

    run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 2\n", "replace_text is the targeted tool, never blocked"


class ThinkingLlm(StubLlm):
    """A stub whose client has already recognised a reasoning endpoint.

    The real `agent.llm.HttpLlm` sets `_thinking_endpoint` from the evidence in a reply
    (`tests/test_llm_thinking.py` pins which). The loop only reads the conclusion, so a
    stub carrying the flag is the honest way to test what the loop does with it.
    """

    _thinking_endpoint: bool = True


def test_a_timeout_with_no_payload_lifts_the_long_llm_clamp(tmp_path: Path) -> None:
    # Case (a): the first call of an episode has no reply to look at, so the timeout is
    # the signal. Lifting the clamp here is what stops the *second* call being clamped and
    # truncated as well.
    target = tmp_path / "out.txt"
    state = make_state(tmp_path)
    assert state.budget.allow_long_llm is False
    llm = StubLlm(replies=[
        LlmError("transport timeout: TimeoutError", retryable=True, timed_out=True),
        action("write_file", path=str(target), content="x"),
        action("finish", path=str(target), verify="test -s out.txt", summary="done"),
    ])

    run(state, llm, FakeVerify((True, "c", "")))

    assert state.budget.allow_long_llm is True
    assert state.budget.llm_timeout_s() <= limits.LLM_THINKING_TIMEOUT_S


def test_a_client_that_recognised_thinking_lifts_the_clamp_on_the_first_reply(
    tmp_path: Path,
) -> None:
    # The other half of "as early as the signal allows": the reply parsed fine, but it
    # carried a reasoning field, so the very next call already gets the raised ceiling.
    state = make_state(tmp_path)
    llm = ThinkingLlm(replies=[action("list_dir", path=str(tmp_path))])

    run(state, llm, FakeVerify())

    assert state.budget.allow_long_llm is True


def test_a_bare_length_truncation_no_longer_lifts_the_clamp(tmp_path: Path) -> None:
    """`F4-7b-regression-4080`: the false positive that cost 250 s on the fast 7B.

    A reply cut off by length, with the client seeing no sign of reasoning behind it, is a
    model looping. The client owns that judgement now, so an error text alone must not
    move the loop's clamp.
    """
    state = make_state(tmp_path)
    llm = StubLlm(replies=[
        LlmError("reply truncated by length at style native_tools", retryable=False),
        action("list_dir", path=str(tmp_path)),
    ])

    run(state, llm, FakeVerify())

    assert state.budget.allow_long_llm is False


def test_a_reasoning_runaway_is_not_by_itself_a_verdict_about_the_endpoint(
    tmp_path: Path,
) -> None:
    """The runaway flag changes the next request; it does not change the call ceiling.

    Those are two different readings of one reply and they have different owners. Whether
    the endpoint reasons before answering is `agent.llm`'s sticky verdict, and on a real
    client it is already set by the time a runaway comes back -- so a loop that also
    inferred it from the flag would be guessing where it has an answer. Pinned because
    the two live one line apart (`F4-7b-regression-4080` is what the guessing costs).
    """
    state = make_state(tmp_path)
    llm = StubLlm(replies=[
        LlmError(
            "reply truncated by length at style native_tools",
            retryable=False,
            output_tokens=limits.LLM_THINKING_MAX_OUTPUT_TOKENS,
            truncated_reasoning=True,
        ),
        action("list_dir", path=str(tmp_path)),
    ])

    run(state, llm, FakeVerify())

    assert state.budget.allow_long_llm is False


def test_a_plain_endpoint_error_does_not_lift_the_clamp(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    llm = StubLlm(replies=[LlmError("connection refused", retryable=True)] * 4)

    run(state, llm, FakeVerify())

    assert state.budget.allow_long_llm is False, "a non-timeout error is not a thinking signal"


def test_timeouts_on_a_slow_endpoint_end_the_run_on_budget_not_as_unusable(
    tmp_path: Path,
) -> None:
    """`F4-30b-paired`: two timeouts demoted a run whose endpoint was still generating.

    A timeout with no payload says the model is slow; a refused connection or an
    unparseable reply says the endpoint cannot be used. Reporting the first as
    `llm-unusable` is a claim about the endpoint that the evidence does not support, and
    it ends a run that still had budget. The clock is what should end it, and does.
    """
    now = [0.0]
    state = make_state(tmp_path, clock=lambda: now[0])

    class TimingOutLlm(StubLlm):
        _thinking_endpoint = True

        def complete_structured(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            now[0] += kwargs.get("timeout_s") or 1.0
            return LlmError(reason="transport timeout: TimeoutError", retryable=True, timed_out=True)

    outcome = run(state, TimingOutLlm(), FakeVerify())

    assert outcome.stop_reason == "budget"
    assert outcome.solved is False


def test_hard_failures_still_reach_llm_unusable_on_a_slow_endpoint(tmp_path: Path) -> None:
    # The counter is per kind, not switched off: an endpoint that answers nothing usable
    # is still declared unusable even after it has been recognised as slow.
    state = make_state(tmp_path)
    llm = ThinkingLlm(replies=[LlmError("connection refused", retryable=True)] * 6)

    outcome = run(state, llm, FakeVerify())

    assert outcome.stop_reason == "llm-unusable"
    assert len(llm.calls) == limits.MAX_CONSECUTIVE_LLM_FAILURES


def test_a_delivered_reply_resets_the_unusable_counter(tmp_path: Path) -> None:
    # Two hard failures, an answer, two more: the endpoint answered in between, so the
    # run must not be ended as unusable on the fourth failure.
    state = make_state(tmp_path)
    state.budget.max_steps = 6
    llm = StubLlm(replies=[
        LlmError("connection refused", retryable=True),
        LlmError("connection refused", retryable=True),
        action("list_dir", path=str(tmp_path)),
        LlmError("connection refused", retryable=True),
        LlmError("connection refused", retryable=True),
        action("list_dir", path=str(tmp_path)),
    ])

    outcome = run(state, llm, FakeVerify())

    assert outcome.stop_reason != "llm-unusable"


def test_a_non_code_fix_playbook_never_blocks_a_blind_write(tmp_path: Path) -> None:
    target = tmp_path / "notes.py"
    target.write_text("old = 1\n", encoding="utf-8")
    state = _codefix_state(tmp_path, target, max_steps=1)
    llm = StubLlm(replies=[action("write_file", path=str(target), content="new = 2\n")])

    run(state, llm, FakeVerify(), playbook=playbooks.FORENSICS)

    assert target.read_text(encoding="utf-8") == "new = 2\n"


def test_the_evaluator_inode_scan_skips_vendored_and_cache_trees(tmp_path: Path) -> None:
    # The alias scan rglob'd all of /app including .venv on every code-fix write
    # (~9.5 s each). Nothing an evaluator file can hide in lives in a skipped dir, so
    # the walk prunes them -- large savings, same guard.
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "vendored.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "app.pyc").write_bytes(b"\x00")

    files = loop._workspace_files(tmp_path)
    names = {p.name for p in files}

    assert "app.py" in names
    assert "vendored.py" not in names, ".venv is not walked"
    assert "app.pyc" not in names, "__pycache__ is not walked"


def test_write_json_writes_a_structured_deliverable_the_model_never_escaped(tmp_path: Path) -> None:
    # The loop must carry the model's structured value to write_json intact (a dict, not
    # a stringified blob) so the finding's embedded quotes survive as valid JSON.
    target = tmp_path / "security_report.json"
    report = {"findings": [{"title": "SQL injection", "evidence": "username = '{req.username}'"}]}
    llm = StubLlm(
        replies=[
            action("write_json", path=str(target), data=report),
            action("finish", path=str(target), verify="test -s security_report.json", summary="done"),
        ]
    )
    verify = FakeVerify((True, "command", ""))

    outcome = run(make_state(tmp_path), llm, verify)

    assert outcome.solved is True
    assert outcome.deliverable == target
    assert json.loads(target.read_text(encoding="utf-8")) == report


def test_a_successful_file_edit_advances_the_state_epoch(tmp_path: Path) -> None:
    target = tmp_path / "source.py"
    target.write_text("before", encoding="utf-8")
    state = make_state(tmp_path)
    state.budget.max_steps = 2
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content="after"),
            action("read_range", path=str(target), start_line=1),
        ]
    )

    run(state, llm, FakeVerify())

    assert state.state_epoch == 1


def test_shell_state_change_classifier_is_conservative() -> None:
    assert loop._shell_can_change("ls /app") is False
    assert loop._shell_can_change("cat app.py > copy.py") is True
    assert loop._shell_can_change("touch marker") is True
    assert loop._shell_can_change("python3 -c 'write()'") is True


def test_finish_pointing_at_a_file_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    # M13: every public verifier reads the environment, never the agent's answer. A
    # model declaring success without an artifact is the single most expensive lie.
    llm = StubLlm(
        replies=[
            action("finish", path=str(tmp_path / "ghost.txt"), verify="", summary="сделано"),
            action("write_file", path=str(tmp_path / "ghost.txt"), content="real"),
            action("finish", path=str(tmp_path / "ghost.txt"), verify="", summary="сделано"),
        ]
    )

    outcome = run(make_state(tmp_path), llm, FakeVerify((True, "existence", "")))

    assert outcome.solved is True, "the run recovers once the file actually exists"
    assert (tmp_path / "ghost.txt").exists()
    assert any("does not exist" in text for text in outcome.observations)


def test_a_red_verifier_spends_the_recovery_pass_and_can_still_win(tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content="{}"),
            action("finish", path=str(target), verify="pytest", summary="v1"),
            action("write_file", path=str(target), content='{"findings": []}'),
            action("finish", path=str(target), verify="pytest", summary="v2"),
        ]
    )
    verify = FakeVerify((False, "command", "exit 1: assert findings"), (True, "command", ""))
    state = make_state(tmp_path)

    outcome = run(state, llm, verify)

    assert outcome.solved is True
    assert state.budget.recovery_passes_used == 1
    assert any("assert findings" in text for text in outcome.observations), (
        "the failure has to reach the model, or the retry is a coin flip"
    )


def test_a_red_verifier_without_recovery_budget_reports_failure_not_success(
    tmp_path: Path,
) -> None:
    target = tmp_path / "report.json"
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content="{}"),
            action("finish", path=str(target), verify="pytest", summary="v1"),
        ]
    )
    state = make_state(tmp_path)
    state.budget.recovery_passes_used = state.budget.max_recovery_passes

    outcome = run(state, llm, FakeVerify((False, "command", "exit 1")))

    assert outcome.solved is False
    assert outcome.stop_reason == "verify-failed"
    assert "exit 1" in outcome.summary


def test_the_same_failing_call_is_refused_before_it_runs_a_third_time(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "file.txt"
    llm = StubLlm(replies=[action("read_range", path=str(missing)) for _ in range(6)])

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    refusals = [text for text in outcome.observations if "already failed" in text]
    assert refusals, "a model repeating a broken call must be told, not obeyed"
    assert outcome.solved is False


def test_a_loop_that_goes_in_circles_is_told_so_and_bounded_by_the_caps(
    tmp_path: Path,
) -> None:
    """Going in circles is no longer an ending, so the caps have to be the ending.

    E028's factor turned the stuck detector into advice; what must not change is that
    a model repeating itself still terminates, on the step cap or the clock rather
    than on the detector's own verdict.
    """
    llm = StubLlm(replies=[action("list_dir", path=str(tmp_path)) for _ in range(40)])

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert outcome.solved is False
    assert outcome.stop_reason in {"steps", "budget"}
    assert len(llm.calls) <= limits.MAX_STEPS


def test_the_step_cap_ends_a_model_that_never_finishes(tmp_path: Path) -> None:
    # A model that keeps writing new files is making progress by every observable
    # measure, so on a clock with time to spare it earns the one step extension
    # (`tests/test_loop_steps.py`) -- and then stops at the raised cap exactly as it
    # used to stop at the original one. The cap is still what ends this run.
    cap = limits.MAX_STEPS + limits.STEP_EXTENSION
    llm = StubLlm(
        replies=[
            action("write_file", path=str(tmp_path / f"f{index}.txt"), content=f"{index}")
            for index in range(cap + 5)
        ]
    )
    state = make_state(tmp_path)

    outcome = run(state, llm, FakeVerify())

    assert state.budget.steps_used == cap
    assert outcome.stop_reason == "steps"
    assert "extended once" in outcome.summary


def test_the_loop_never_starts_a_step_inside_the_verifier_reserve(tmp_path: Path) -> None:
    now = [0.0]

    def clock() -> float:
        return now[0]

    state = make_state(tmp_path, clock=clock)
    llm = StubLlm(replies=[action("list_dir", path=str(tmp_path)) for _ in range(10)])

    # Jump to the moment only the reserve is left before the first step can run.
    now[0] = state.budget.total_s - state.budget.verify_reserve_s

    outcome = run(state, llm, FakeVerify())

    assert state.budget.steps_used == 0
    assert outcome.stop_reason == "budget"
    assert llm.calls == [], "no tokens are spent once there is only reserve left"


def test_output_that_is_not_json_is_answered_not_crashed_on(tmp_path: Path) -> None:
    llm = StubLlm(
        replies=[
            LlmReply(text="Конечно! Сейчас посмотрю файлы."),
            # A file and not `tmp_path` itself: the subject here is the malformed
            # reply, and a finish aimed at a directory is now refused on its own.
            action("finish", path=str(tmp_path / "x"), verify="", summary="ok"),
        ]
    )
    (tmp_path / "x").write_text("x", encoding="utf-8")

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert any("json" in text.lower() for text in outcome.observations)
    assert outcome.solved is True


def test_an_unknown_tool_name_comes_back_as_a_typed_error(tmp_path: Path) -> None:
    llm = StubLlm(replies=[action("sudo_rm"), action("list_dir", path=str(tmp_path))])

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert any("unknown tool" in text for text in outcome.observations)


def test_an_endpoint_that_keeps_failing_ends_the_run_instead_of_hanging(tmp_path: Path) -> None:
    llm = StubLlm(replies=[LlmError(reason="connection refused", retryable=True)] * 40)

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert outcome.solved is False
    assert outcome.stop_reason == "llm-unusable"
    assert len(llm.calls) <= limits.MAX_STEPS


def test_past_the_early_deadline_the_model_is_told_to_produce(tmp_path: Path) -> None:
    now = [0.0]
    state = make_state(tmp_path, clock=lambda: now[0])
    llm = StubLlm(replies=[action("list_dir", path=str(tmp_path)) for _ in range(3)])
    now[0] = limits.EARLY_DELIVERABLE_DEADLINE_S + 1

    outcome = run(state, llm, FakeVerify())

    assert any("нет проверяемого результата" in text for text in outcome.observations)


def test_an_artifact_left_without_finish_is_still_offered_to_the_verifier(
    tmp_path: Path,
) -> None:
    # Being killed by the step cap must cost the refinement, never the deliverable.
    target = tmp_path / "left.txt"
    llm = StubLlm(replies=[action("write_file", path=str(target), content="partial")])
    verify = FakeVerify((True, "existence", ""))

    outcome = run(make_state(tmp_path), llm, verify)

    assert verify.calls == [(target, "")]
    assert outcome.deliverable == target


def test_a_secret_never_reaches_the_trace_or_an_observation(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.secrets = ("sk-canary-value",)
    leak = tmp_path / "leak.txt"
    leak.write_text("OPENAI_API_KEY=sk-canary-value\n", encoding="utf-8")
    llm = StubLlm(replies=[action("read_range", path=str(leak)), action("finish", path=str(leak))])

    outcome = run(state, llm, FakeVerify())

    assert "sk-canary-value" not in "\n".join(outcome.observations)
    assert "sk-canary-value" not in "\n".join(event.detail for event in state.audit)


def test_every_step_leaves_a_trace_entry_with_its_tool_and_outcome(tmp_path: Path) -> None:
    llm = StubLlm(replies=[action("list_dir", path=str(tmp_path))])

    state = make_state(tmp_path)
    run(state, llm, FakeVerify())

    tools = [event for event in state.audit if event.kind == "tool"]
    assert tools and tools[0].name == "list_dir"
    assert tools[0].duration_s >= 0.0


def test_a_run_that_never_reached_finish_is_never_reported_as_solved(tmp_path: Path) -> None:
    # The most expensive kind of wrong answer is a quiet success. `unverified` means
    # "a non-empty file is there", which is the right answer when the model chose to
    # finish without a self-check -- and the wrong one when it never got that far. A
    # run killed by the step cap has proven nothing, whatever is lying on disk.
    target = tmp_path / "half-written.txt"
    llm = StubLlm(replies=[action("write_file", path=str(target), content="partial")])
    verify = FakeVerify((True, "unverified", "exists and is non-empty"))

    outcome = run(make_state(tmp_path), llm, verify)

    assert outcome.solved is False, "no explicit finish means nothing was proven"
    assert outcome.deliverable == target, "the artifact is still worth reporting"
    assert outcome.verdict is not None and outcome.verdict.stage == "unverified"


def test_a_verify_command_that_only_reads_the_deliverable_is_refused(tmp_path: Path) -> None:
    """The two false wins the night reproduced 5/5 each, and the same command both times.

    `mut-greeting-farewell` had to write the four bytes `Ciao`; the model wrote `43616f`
    and offered `cat /srv/bootstrap/farewell.marker` as its check. `ctf-signed-cookie`
    offered `cat /app/flag.txt` while its own summary said the proof string had probably
    not been released. `cat` on a file the finish gate has already found and already
    called non-empty exits 0 whatever the bytes are, so both runs stopped at a green
    self-check with two thirds of the budget unspent and scored zero.

    A defect in the shape of the claim, so it is refused where a directory is refused
    and costs no recovery pass: what the check contains is still `agent.verify`'s.
    """
    target = tmp_path / "farewell.marker"
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content="43616f"),
            action("finish", path=str(target), verify=f"cat {target}", summary="готово"),
            action("write_file", path=str(target), content="Ciao"),
            action("finish", path=str(target), verify=f"grep -qx Ciao {target}", summary="готово"),
        ]
    )
    verify = FakeVerify((True, "command", ""))
    state = make_state(tmp_path)

    outcome = run(state, llm, verify)

    assert [call[1] for call in verify.calls] == [f"grep -qx Ciao {target}"], (
        "the reading command never reaches the verifier"
    )
    assert state.budget.recovery_passes_used == 0, "a check that is not a check is not a red check"
    assert outcome.solved is True, "the run recovers once it offers a real check"


def test_a_verify_command_whose_failure_is_swallowed_is_refused(tmp_path: Path) -> None:
    """E031's new false win, and the first one written *after* a red verdict.

    `audit-hardcoded-token` offered a real check first, was told no, and came back with
    `grep -q … && echo 'PASS' || echo 'FAIL'`. The `|| echo` makes the exit code 0
    whatever `grep` decided, so the run stopped as `verified` holding a wrong report.
    The model repaired the check instead of the result, which is the one thing the
    project rules say a run must never do.
    """
    target = tmp_path / "audit_report.json"
    swallowed = f"grep -q secret {target} && echo 'PASS' || echo 'FAIL'"
    honest = f"grep -q secret {target}"
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content="{}"),
            action("finish", path=str(target), verify=swallowed, summary="готово"),
            action("write_file", path=str(target), content="secret"),
            action("finish", path=str(target), verify=honest, summary="готово"),
        ]
    )
    verify = FakeVerify((True, "command", ""))
    state = make_state(tmp_path)

    outcome = run(state, llm, verify)

    assert [call[1] for call in verify.calls] == [honest], (
        "a command whose failure branch always exits 0 never reaches the verifier"
    )
    assert state.budget.recovery_passes_used == 0
    assert outcome.solved is True


def test_a_pipeline_of_readers_is_refused_like_a_lone_reader(tmp_path: Path) -> None:
    """The fifth shape: `cat marker | od -A x -t x1z -v`, seen once the lone reader was
    refused. Every stage is a reader, nothing compares anything, and the exit code is
    the last stage's -- zero for any bytes the file holds. The pipe made it look like a
    real check to a rule that let anything with a `|` through.
    """
    target = tmp_path / "farewell.marker"
    piped = f"cat {target} | od -A x -t x1z -v"
    honest = f"grep -qx Ciao {target}"
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content="43616f"),
            action("finish", path=str(target), verify=piped, summary="готово"),
            action("write_file", path=str(target), content="Ciao"),
            action("finish", path=str(target), verify=honest, summary="готово"),
        ]
    )
    verify = FakeVerify((True, "command", ""))
    state = make_state(tmp_path)

    outcome = run(state, llm, verify)

    assert [call[1] for call in verify.calls] == [honest], (
        "a pipeline that only reads never reaches the verifier"
    )
    assert state.budget.recovery_passes_used == 0
    assert outcome.solved is True


def test_a_pipeline_that_compares_something_is_left_alone(tmp_path: Path) -> None:
    """The rule narrows the pipe exemption; it does not withdraw it. A pipeline with a
    stage that can disagree with the file is the check the gate exists to protect.
    """
    target = tmp_path / "findings.json"
    command = f"cat {target} | grep -q sql_injection"
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content="sql_injection"),
            action("finish", path=str(target), verify=command, summary="готово"),
        ]
    )
    verify = FakeVerify((True, "command", ""))

    outcome = run(make_state(tmp_path), llm, verify)

    assert outcome.solved is True
    assert [call[1] for call in verify.calls] == [command]


def test_a_verify_command_that_can_fail_is_left_alone(tmp_path: Path) -> None:
    """The one true win of the night passed `grep -q … && grep -q …`, and every other
    command the model proposed contained `grep`, `python3` or a comparison. Only a lone
    reader is refused: rejecting a real check would cost the task the gate exists for.
    """
    target = tmp_path / "findings.json"
    command = f"grep -q sql_injection {target} && grep -q authorize {target}"
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content="sql_injection authorize"),
            action("finish", path=str(target), verify=command, summary="готово"),
        ]
    )
    verify = FakeVerify((True, "command", ""))

    outcome = run(make_state(tmp_path), llm, verify)

    assert outcome.solved is True
    assert [call[1] for call in verify.calls] == [command]


def test_a_stuck_run_holding_a_readable_file_is_still_a_zero(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("something", encoding="utf-8")
    llm = StubLlm(replies=[action("list_dir", path=str(tmp_path)) for _ in range(40)])
    state = make_state(tmp_path)
    state.artifacts.append(target)

    outcome = run(state, llm, FakeVerify((True, "unverified", "")))

    assert outcome.stop_reason == "stuck"
    assert outcome.solved is False


def test_code_fix_does_not_finish_with_only_a_report_artifact(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("answer = 1\n", encoding="utf-8")
    report = tmp_path / "README.md"
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action("write_file", path=str(report), content="changed report"),
            action("finish", path=str(report), verify="true", summary="fixed"),
            LlmError(reason="no more", retryable=False),
        ]
    )
    verify = FakeVerify((True, "command", ""))

    outcome = run(state, llm, verify, playbook=playbooks.CODE_FIX)

    assert outcome.solved is False
    assert verify.calls == []
    assert any("production source" in text for text in outcome.observations)


def test_code_fix_does_not_stop_on_stuck_with_only_a_report_artifact(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("answer = 1\n", encoding="utf-8")
    report = tmp_path / "README.md"
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    repeated = action("list_dir", path=str(tmp_path))
    llm = StubLlm(
        replies=[action("write_file", path=str(report), content="changed report")]
        + [repeated for _ in range(limits.NO_PROGRESS_STOP + 4)]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert outcome.stop_reason != "stuck"
    assert any("production source" in text for text in outcome.observations)


def test_code_fix_can_finish_after_a_source_edit(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action("replace_text", path=str(target), old="answer = 1", new="answer = 2"),
            action("finish", path=str(target), verify="python3 -c ''", summary="fixed"),
        ]
    )

    outcome = run(state, llm, FakeVerify((True, "command", "")), playbook=playbooks.CODE_FIX)

    assert outcome.solved is True
    assert outcome.deliverable == target


def test_code_fix_cannot_mutate_evaluator_tests(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "replace_text",
                path=str(target),
                old="assert status == 200",
                new="assert status == 400",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="hardlink semantics differ on Windows")
@pytest.mark.parametrize("tool", ["write_file", "replace_text"])
def test_code_fix_cannot_edit_evaluator_through_hardlink(
    tmp_path: Path, tool: str
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    if tool == "write_file":
        reply = action(tool, path=str(alias), content="value = 900\n")
    else:
        reply = action(
            tool,
            path=str(alias),
            old="value = 200",
            new="value = 900",
        )
    outcome = run(state, StubLlm(replies=[reply]), FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="hardlink semantics differ on Windows")
def test_code_fix_cannot_redirect_to_external_named_evaluator_through_hardlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    target = tmp_path / "srv" / "tests" / "test_api.py"
    target.parent.mkdir(parents=True)
    target.write_text("value = 200\n", encoding="utf-8")
    alias = workspace / "app.py"
    alias.hardlink_to(target)
    state = AgentState(
        instruction=f"Fix the bug in {target}.",
        workspace=workspace,
        budget=Budget(total_s=600.0, clock=lambda: 0.0),
        named_paths=(target,),
    )
    state.source_snapshot = result_progress.snapshot_sources(workspace)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="printf 'value = 900\\n' > app.py",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_the_evaluator_path_guard_is_scoped_to_code_fix(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.txt"
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(replies=[action("write_file", path=str(target), content="data")])

    run(state, llm, FakeVerify(), playbook=playbooks.GENERAL)

    assert target.read_text(encoding="utf-8") == "data"


def test_code_fix_cannot_mutate_evaluator_tests_through_shell(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"printf 'assert status = 400\\n' > '{target}'",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_guard_covers_named_evaluator_paths_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    target = tmp_path / "srv" / "tests" / "test_api.py"
    target.parent.mkdir(parents=True)
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = AgentState(
        instruction=f"Fix the bug in {target}.",
        workspace=workspace,
        budget=Budget(total_s=600.0, clock=lambda: 0.0),
        named_paths=(target,),
    )
    state.source_snapshot = result_progress.snapshot_sources(workspace)
    llm = StubLlm(
        replies=[action("write_file", path=str(target), content="assert status == 400\n")]
    )

    outcome = loop.run(
        state,
        llm,
        FakeVerify(),
        brief=make_brief(workspace),
        playbook=playbooks.CODE_FIX,
    )

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_hide_evaluator_mutation_in_find_exec(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"find '{target.parent}' -name test_api.py -exec sed -i 's/200/400/' {{}} +",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_mutate_evaluator_tests_through_find_glob_delete(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="find . -name 'test_*.py' -delete",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_mutate_evaluator_tests_through_shell_glob(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="sed -i 's/200/400/' t*sts/fixture.py",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_find_fprint_on_evaluator_tests(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"find '{target.parent}' -type f -fprint '{target}'",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_find_fprint0_on_evaluator_tests(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"find '{target.parent}' -type f -fprint0 '{target}'",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_workspace_named_tests_does_not_block_production_source(tmp_path: Path) -> None:
    workspace = tmp_path / "tests"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    state = make_state(workspace)
    state.source_snapshot = result_progress.snapshot_sources(workspace)
    state.budget.max_steps = 2
    # read_range first: the unread-source guard now requires the run to have opened the
    # file before overwriting it. The point of this test -- that a `tests/` workspace
    # does not make its production source an evaluator path -- is unchanged.
    llm = StubLlm(replies=[
        action("read_range", path=str(target), start_line=1),
        action("write_file", path=str(target), content="answer = 2\n"),
    ])

    run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "answer = 2\n"


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_workspace_named_tests_does_not_block_production_shell_edit(tmp_path: Path) -> None:
    workspace = tmp_path / "tests"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    state = make_state(workspace)
    state.source_snapshot = result_progress.snapshot_sources(workspace)
    state.budget.max_steps = 1
    llm = StubLlm(
        replies=[action("run_shell", command=f"printf 'answer = 2\\n' > '{target}'")]
    )

    run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "answer = 2\n"


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_allows_quoted_shell_separator_in_production_write(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"printf 'value = 2;\\n' > '{target}'",
            )
        ]
    )

    run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 2;\n"


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
def test_code_fix_cannot_redirect_through_source_symlink_into_evaluator(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    source = tmp_path / "app.py"
    source.symlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="printf 'value = 900\\n' > app.py",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="hardlink semantics differ on Windows")
def test_code_fix_cannot_redirect_through_hardlink_into_evaluator(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="printf 'value = 900\\n' > app.py",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="hardlink semantics differ on Windows")
def test_code_fix_cannot_redirect_through_nested_shell_into_evaluator(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command='sh -c "echo value > app.py"',
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="hardlink semantics differ on Windows")
def test_code_fix_cannot_redirect_through_combined_shell_option_into_evaluator(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command='sh -xc "echo value > app.py"',
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell builtins differ on Windows")
def test_code_fix_cannot_hide_evaluator_mutation_behind_builtin_eval(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="builtin eval 'printf bad > app.py'",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="hardlink semantics differ on Windows")
def test_code_fix_cannot_redirect_through_env_alias_into_evaluator(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="env TARGET=app.py sh -c 'printf bad > \"$TARGET\"'",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="hardlink semantics differ on Windows")
def test_code_fix_cannot_redirect_through_pwd_env_alias_into_evaluator(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="env TARGET=\"$PWD/app.py\" sh -c 'printf bad > \"$TARGET\"'",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="hardlink semantics differ on Windows")
def test_code_fix_cannot_hide_evaluator_redirect_in_trap_handler(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="trap 'printf bad > app.py' 0",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="hardlink semantics differ on Windows")
def test_code_fix_cannot_hide_evaluator_mutation_in_env_split_string(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command='env -S "truncate -s 0 app.py"',
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="hardlink semantics differ on Windows")
def test_code_fix_cannot_hide_evaluator_mutation_in_opaque_runner(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command='parallel "truncate -s 0 app.py"',
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="hardlink semantics differ on Windows")
def test_code_fix_cannot_hide_evaluator_mutation_in_bash_env_hook(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    hook = tmp_path / "payload.sh"
    hook.write_text("printf bad > app.py\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="BASH_ENV=payload.sh bash -c :",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Bash startup semantics differ on Windows")
def test_code_fix_cannot_hide_evaluator_mutation_in_prompt_command(
    tmp_path: Path,
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    "PROMPT_COMMAND='printf pwned > app.py' "
                    f"{bash} -i"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX zsh startup semantics differ on Windows")
def test_code_fix_cannot_hide_evaluator_mutation_in_zdotdir(
    tmp_path: Path,
) -> None:
    zsh = shutil.which("zsh")
    if zsh is None:
        pytest.skip("zsh is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    zdot = tmp_path / "zdot"
    zdot.mkdir()
    (zdot / ".zshrc").write_text("printf pwned > app.py\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"ZDOTDIR=./zdot {zsh} -i",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX dynamic loader semantics differ on Windows")
def test_code_fix_cannot_inject_code_through_ld_audit(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        pytest.skip("a C compiler is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "audit.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "audit.so", "audit.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("the local C compiler cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="LD_AUDIT=./audit.so /bin/true",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX glibc loader semantics differ on Windows")
def test_code_fix_cannot_load_gconv_module_through_environment(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("cc") or shutil.which("gcc")
    iconv = shutil.which("iconv")
    if compiler is None or iconv is None:
        pytest.skip("a C compiler and iconv are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    gconv = tmp_path / "gconv"
    gconv.mkdir()
    (gconv / "gconv-modules").write_text(
        "module PWN// UTF-8// payload 1\n",
        encoding="utf-8",
    )
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", str(gconv / "payload.so"), "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("the local C compiler cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"GCONV_PATH=./gconv {iconv} -f PWN -t UTF-8",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX dynamic loader semantics differ on Windows")
def test_code_fix_cannot_inject_code_through_direct_dynamic_loader(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("cc") or shutil.which("gcc")
    loader = next(
        (
            candidate
            for directory in (Path("/lib64"), Path("/lib"), Path("/lib/x86_64-linux-gnu"))
            for candidate in directory.glob("ld-linux*.so.*")
            if candidate.is_file()
        ),
        None,
    )
    if compiler is None or loader is None:
        pytest.skip("a C compiler and a dynamic loader are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("the local C compiler cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{loader} --preload ./payload.so /bin/true",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX dynamic loader semantics differ on Windows")
def test_code_fix_cannot_hide_dynamic_loader_behind_symlink(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("cc") or shutil.which("gcc")
    loader = next(
        (
            candidate
            for directory in (Path("/lib64"), Path("/lib"), Path("/lib/x86_64-linux-gnu"))
            for candidate in directory.glob("ld-linux*.so.*")
            if candidate.is_file()
        ),
        None,
    )
    if compiler is None or loader is None:
        pytest.skip("a C compiler and a dynamic loader are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("the local C compiler cannot build a shared object")
    runner = tmp_path / "runner"
    runner.symlink_to(loader)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="./runner --preload ./payload.so /bin/true",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX sqlite3 semantics differ on Windows")
def test_code_fix_cannot_execute_sqlite_shell_meta_command(
    tmp_path: Path,
) -> None:
    sqlite3 = shutil.which("sqlite3")
    if sqlite3 is None:
        pytest.skip("sqlite3 is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f'{sqlite3} :memory: ".shell printf mutated > app.py"',
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Bash semantics differ on Windows")
def test_code_fix_cannot_import_bash_exported_function(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    "env 'BASH_FUNC_pwn%%=() { printf mutated > app.py; }' "
                    "bash -c pwn"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX OpenSSL loader semantics differ on Windows")
def test_code_fix_cannot_load_openssl_dynamic_engine(
    tmp_path: Path,
) -> None:
    openssl = shutil.which("openssl")
    compiler = shutil.which("cc") or shutil.which("gcc")
    if openssl is None or compiler is None:
        pytest.skip("openssl and a C compiler are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("the local C compiler cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{openssl} engine dynamic -pre SO_PATH:./payload.so "
                    "-pre ID:payload -pre LIST_ADD:1 -pre LOAD"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="xargs semantics differ on Windows")
def test_code_fix_cannot_use_path_qualified_xargs_for_evaluator_mutation(
    tmp_path: Path,
) -> None:
    xargs = shutil.which("xargs")
    if xargs is None:
        pytest.skip("xargs is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    payload = tmp_path / "payload.txt"
    payload.write_text("app.py\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{xargs} -a payload.txt truncate -s 0",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX command wrappers differ on Windows")
@pytest.mark.parametrize("wrapper", ["nice", "time", "timeout"])
def test_code_fix_cannot_hide_evaluator_mutation_behind_command_wrapper(
    tmp_path: Path, wrapper: str
) -> None:
    executable = shutil.which(wrapper)
    if executable is None:
        pytest.skip(f"{wrapper} is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    prefix = f"{executable} 5" if wrapper == "timeout" else executable
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f'{prefix} awk \'BEGIN {{ system("truncate -s 0 app.py") }}\'',
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="flock semantics differ on Windows")
def test_code_fix_cannot_hide_evaluator_mutation_behind_flock(
    tmp_path: Path,
) -> None:
    flock = shutil.which("flock")
    if flock is None:
        pytest.skip("flock is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f'{flock} -n lockfile awk \'BEGIN {{ system("truncate -s 0 app.py") }}\'',
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="setpriv semantics differ on Windows")
def test_code_fix_cannot_hide_evaluator_mutation_behind_setpriv(
    tmp_path: Path,
) -> None:
    setpriv = shutil.which("setpriv")
    if setpriv is None:
        pytest.skip("setpriv is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f'{setpriv} -- awk \'BEGIN {{ system("truncate -s 0 app.py") }}\'',
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="prlimit semantics differ on Windows")
def test_code_fix_cannot_hide_evaluator_mutation_behind_prlimit(
    tmp_path: Path,
) -> None:
    prlimit = shutil.which("prlimit")
    if prlimit is None:
        pytest.skip("prlimit is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f'{prlimit} -- awk \'BEGIN {{ system("truncate -s 0 app.py") }}\'',
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX zip semantics differ on Windows")
def test_code_fix_cannot_execute_zip_test_command(
    tmp_path: Path,
) -> None:
    zip_tool = shutil.which("zip")
    if zip_tool is None:
        pytest.skip("zip is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "source.txt").write_text("source\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{zip_tool} -q archive.zip source.txt -T -TT "
                    "'printf mutated > app.py'"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX tar semantics differ on Windows")
def test_code_fix_cannot_execute_tar_checkpoint_action(
    tmp_path: Path,
) -> None:
    tar = shutil.which("tar")
    if tar is None:
        pytest.skip("tar is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "source.txt").write_text("source\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{tar} -cf archive.tar --checkpoint=1 "
                    "--checkpoint-action=exec='printf mutated > app.py' source.txt"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX SSH semantics differ on Windows")
def test_code_fix_cannot_execute_ssh_proxy_command(
    tmp_path: Path,
) -> None:
    ssh = shutil.which("ssh")
    if ssh is None:
        pytest.skip("ssh is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{ssh} -o 'ProxyCommand=sh -c \"printf mutated > app.py\"' "
                    "-o ConnectTimeout=1 nowhere.invalid true"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX SSH semantics differ on Windows")
def test_code_fix_cannot_run_command_through_ssh_agent(
    tmp_path: Path,
) -> None:
    ssh_agent = shutil.which("ssh-agent")
    if ssh_agent is None:
        pytest.skip("ssh-agent is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{ssh_agent} python3 -c \"open('app.py', 'w').write('bad\\n')\""
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX SSH loader semantics differ on Windows")
def test_code_fix_cannot_load_ssh_pkcs11_provider(
    tmp_path: Path,
) -> None:
    ssh_keygen = shutil.which("ssh-keygen")
    compiler = shutil.which("cc") or shutil.which("gcc")
    if ssh_keygen is None or compiler is None:
        pytest.skip("ssh-keygen and a C compiler are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n"
        "int C_GetFunctionList(void *unused) { (void)unused; return 1; }\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("the local C compiler cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{ssh_keygen} -D ./payload.so -lf /dev/null",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ccache semantics differ on Windows")
def test_code_fix_cannot_hide_gcc_plugin_behind_ccache(
    tmp_path: Path,
) -> None:
    ccache = shutil.which("ccache")
    compiler = shutil.which("gcc")
    if ccache is None or compiler is None:
        pytest.skip("ccache and gcc are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("gcc cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{ccache} {compiler} -fplugin=./payload.so -x c -c "
                    "/dev/null -o /tmp/code_fix_ccache_probe.o"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX GCC toolchain semantics differ on Windows")
def test_code_fix_cannot_replace_gcc_toolchain_with_b_prefix(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("gcc")
    if compiler is None:
        pytest.skip("gcc is not installed")
    cc1_result = subprocess.run(
        [compiler, "-print-prog-name=cc1"],
        capture_output=True,
        text=True,
        check=False,
    )
    cc1 = cc1_result.stdout.strip()
    if cc1_result.returncode != 0 or not Path(cc1).is_file():
        pytest.skip("gcc cc1 is unavailable")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "source.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    cc1_wrapper = toolchain / "cc1"
    cc1_wrapper.write_text(
        f"#!/bin/sh\nprintf 'bad\\n' > app.py\nexec {cc1} \"$@\"\n",
        encoding="utf-8",
    )
    cc1_wrapper.chmod(0o755)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{compiler} -B./toolchain -c source.c "
                    "-o /tmp/code_fix_toolchain_probe.o"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX GCC toolchain semantics differ on Windows")
def test_code_fix_cannot_replace_gcc_toolchain_with_environment(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("gcc")
    if compiler is None:
        pytest.skip("gcc is not installed")
    cc1_result = subprocess.run(
        [compiler, "-print-prog-name=cc1"],
        capture_output=True,
        text=True,
        check=False,
    )
    cc1 = cc1_result.stdout.strip()
    if cc1_result.returncode != 0 or not Path(cc1).is_file():
        pytest.skip("gcc cc1 is unavailable")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "source.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    cc1_wrapper = toolchain / "cc1"
    cc1_wrapper.write_text(
        f"#!/bin/sh\nprintf 'bad\\n' > app.py\nexec {cc1} \"$@\"\n",
        encoding="utf-8",
    )
    cc1_wrapper.chmod(0o755)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    "COMPILER_PATH=./toolchain "
                    f"{compiler} -c source.c -o /tmp/code_fix_env_toolchain_probe.o"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX GCC dependency-file semantics differ on Windows")
@pytest.mark.parametrize(
    "map_option",
    [
        "-Wl,-Map,app.py",
        "-Wl,-Map=app.py",
        "-Wl,--dependency-file,app.py",
        "-Wl,--dependency-file=app.py",
    ],
)
def test_code_fix_cannot_write_gcc_forwarded_output_into_evaluator(
    tmp_path: Path,
    map_option: str,
) -> None:
    compiler = shutil.which("gcc")
    if compiler is None:
        pytest.skip("gcc is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    (tmp_path / "source.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{compiler} {map_option} -c source.c "
                    "-o /tmp/code_fix_dep_probe.o"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX GCC linker-script semantics differ on Windows")
def test_code_fix_cannot_use_gcc_linker_script_for_evaluator_output(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("gcc")
    if compiler is None:
        pytest.skip("gcc is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    (tmp_path / "source.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "source.ld").write_text(
        "OUTPUT(tests/fixture.py)\nSECTIONS { .text : { *(.text*) } }\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-c", "source.c", "-o", "source.o"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("gcc cannot build linker fixture")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="gcc -Wl,-T,source.ld source.o",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX linker-script semantics differ on Windows")
@pytest.mark.parametrize("command_kind", ["default", "dt", "forwarded"])
def test_code_fix_cannot_use_linker_script_alias_for_evaluator_output(
    tmp_path: Path,
    command_kind: str,
) -> None:
    linker = shutil.which("ld")
    compiler = shutil.which("gcc")
    if linker is None or compiler is None:
        pytest.skip("ld and gcc are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    (tmp_path / "source.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "source.ld").write_text(
        "OUTPUT(tests/fixture.py)\nSECTIONS { .text : { *(.text*) } }\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-c", "source.c", "-o", "source.o"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("gcc cannot build linker fixture")
    command = {
        "default": f"{linker} --default-script=source.ld source.o",
        "dt": f"{linker} -dT source.ld source.o",
        "forwarded": f"{compiler} -Wl,--default-script,source.ld source.o",
    }[command_kind]
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command=command)])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX GCC response-file semantics differ on Windows")
def test_code_fix_cannot_hide_gcc_output_in_response_file(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("gcc")
    if compiler is None:
        pytest.skip("gcc is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    (tmp_path / "source.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.rsp").write_text(
        "-MD -MFapp.py -c source.c -o /tmp/code_fix_rsp_probe.o\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command=f"{compiler} @payload.rsp")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX MRI linker semantics differ on Windows")
@pytest.mark.parametrize("forwarded", [False, True])
@pytest.mark.parametrize("linker_name", ["ld", "ld.bfd"])
def test_code_fix_cannot_use_ld_mri_script_for_evaluator_output(
    tmp_path: Path,
    forwarded: bool,
    linker_name: str,
) -> None:
    linker = shutil.which(linker_name)
    compiler = shutil.which("cc") or shutil.which("gcc")
    if linker is None or compiler is None:
        pytest.skip(f"{linker_name} and a C compiler are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    (tmp_path / "source.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.mri").write_text(
        "NAME tests/fixture.py\nEND\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-c", "source.c", "-o", "source.o"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("the local compiler cannot build MRI fixture")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{compiler} -Wl,-c,payload.mri source.o"
                    if forwarded
                    else f"{linker} -c payload.mri source.o"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX GCC linker-script semantics differ on Windows")
def test_code_fix_cannot_use_gcc_linker_error_script(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("gcc")
    if compiler is None:
        pytest.skip("gcc is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    (tmp_path / "source.c").write_text(
        "extern int missing_symbol(void);\n"
        "int main(void) { return missing_symbol(); }\n",
        encoding="utf-8",
    )
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    script = tmp_path / "payload.sh"
    script.write_text("printf 'bad\\n' > app.py\n", encoding="utf-8")
    script.chmod(0o755)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{compiler} -Wl,--error-handling-script=./payload.sh "
                    "source.c -o /tmp/code_fix_error_script_probe"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX GCC plugin semantics differ on Windows")
def test_code_fix_cannot_load_gcc_plugin(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("gcc")
    if compiler is None:
        pytest.skip("gcc is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("gcc cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{compiler} -fplugin=./payload.so -x c -c /dev/null "
                    "-o /tmp/code_fix_probe.o"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX GCC linker plugin semantics differ on Windows")
@pytest.mark.parametrize("plugin_option", ["-plugin=./payload.so", "--plugin=./payload.so"])
def test_code_fix_cannot_forward_gcc_plugin_to_linker(
    tmp_path: Path,
    plugin_option: str,
) -> None:
    compiler = shutil.which("gcc")
    if compiler is None:
        pytest.skip("gcc is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("gcc cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{compiler} -Wl,{plugin_option} -x c -o "
                    "/tmp/code_fix_forwarded_plugin /dev/null"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Clang plugin semantics differ on Windows")
@pytest.mark.parametrize(
    "plugin_options",
    [
        "-Xclang -load -Xclang ./payload.so",
        "-fpass-plugin=./payload.so",
        "-Xlinker --plugin -Xlinker ./payload.so",
    ],
)
def test_code_fix_cannot_load_clang_plugin(
    tmp_path: Path,
    plugin_options: str,
) -> None:
    compiler = shutil.which("clang")
    if compiler is None:
        pytest.skip("clang is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("clang cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{compiler} {plugin_options} "
                    "-x c -c /dev/null -o /tmp/code_fix_clang_probe.o"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Clang config semantics differ on Windows")
def test_code_fix_cannot_load_clang_plugin_through_config_file(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("clang")
    if compiler is None:
        pytest.skip("clang is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("clang cannot build a shared object")
    (tmp_path / "payload.cfg").write_text(
        "-fpass-plugin=./payload.so\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{compiler} --config=./payload.cfg -x c -c /dev/null "
                    "-o /tmp/code_fix_config_plugin_probe.o"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Clang environment semantics differ on Windows")
def test_code_fix_cannot_load_clang_plugin_through_override_environment(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("clang")
    if compiler is None:
        pytest.skip("clang is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "source.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("clang cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    "CCC_OVERRIDE_OPTIONS='+-Xclang +-load +-Xclang +./payload.so' "
                    f"{compiler} -c source.c -o /tmp/code_fix_override_plugin_probe.o"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Clang wrapper semantics differ on Windows")
@pytest.mark.parametrize("wrapper", ["env", "command", "exec"])
def test_code_fix_cannot_hide_clang_plugin_behind_simple_wrapper(
    tmp_path: Path,
    wrapper: str,
) -> None:
    compiler = shutil.which("clang")
    if compiler is None:
        pytest.skip("clang is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("clang cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{wrapper} {compiler} -Xclang -load -Xclang ./payload.so "
                    "-x c -c /dev/null -o /tmp/code_fix_wrapper_plugin_probe.o"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Clang driver semantics differ on Windows")
@pytest.mark.parametrize("driver", ["clang-22", "clang-cpp"])
def test_code_fix_cannot_load_clang_alias_plugin(tmp_path: Path, driver: str) -> None:
    compiler = shutil.which(driver)
    if compiler is None:
        pytest.skip(f"{driver} is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip(f"{driver} cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{compiler} -Xclang -load -Xclang ./payload.so "
                    "-x c -c /dev/null -o /tmp/code_fix_versioned_plugin_probe.o"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX GCC binutils semantics differ on Windows")
@pytest.mark.parametrize("driver", ["gcc-nm", "gcc-nm-15"])
def test_code_fix_cannot_load_gcc_nm_plugin(tmp_path: Path, driver: str) -> None:
    gcc_nm = shutil.which(driver)
    compiler = shutil.which("gcc")
    if gcc_nm is None or compiler is None:
        pytest.skip(f"{driver} and gcc are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "source.c").write_text("int value(void) { return 0; }\n", encoding="utf-8")
    built_object = subprocess.run(
        [compiler, "-c", "source.c", "-o", "source.o"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built_object.returncode != 0:
        pytest.skip(f"{driver} fixture object could not be built")
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built_payload = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built_payload.returncode != 0:
        pytest.skip(f"{driver} plugin fixture could not be built")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{gcc_nm} --plugin ./payload.so source.o",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX BFD linker semantics differ on Windows")
def test_code_fix_cannot_load_ld_bfd_plugin(tmp_path: Path) -> None:
    linker = shutil.which("ld.bfd")
    compiler = shutil.which("cc") or shutil.which("gcc")
    if linker is None or compiler is None:
        pytest.skip("ld.bfd and a C compiler are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("the local C compiler cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{linker} -plugin ./payload.so -o /tmp/code_fix_bfd_probe.o /dev/null",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX clang-tidy semantics differ on Windows")
def test_code_fix_cannot_load_clang_tidy_plugin(tmp_path: Path) -> None:
    clang_tidy = shutil.which("clang-tidy")
    compiler = shutil.which("cc") or shutil.which("gcc")
    if clang_tidy is None or compiler is None:
        pytest.skip("clang-tidy and a C compiler are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "source.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("the local C compiler cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{clang_tidy} -load=./payload.so source.c -- -c",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Clang frontend semantics differ on Windows")
def test_code_fix_cannot_load_clang_cc1_plugin(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("clang")
    if compiler is None:
        pytest.skip("clang is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "source.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("clang cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{compiler} -cc1 -load ./payload.so -emit-obj source.c "
                    "-o /tmp/code_fix_cc1_probe.o"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX scan-build semantics differ on Windows")
def test_code_fix_cannot_hide_clang_plugin_behind_scan_build(
    tmp_path: Path,
) -> None:
    scan_build = shutil.which("scan-build")
    compiler = shutil.which("clang")
    if scan_build is None or compiler is None:
        pytest.skip("scan-build and clang are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("clang cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{scan_build} {compiler} -Xclang -load -Xclang ./payload.so "
                    "-c /dev/null -o /tmp/code_fix_scan_build_probe.o"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX intercept-build semantics differ on Windows")
def test_code_fix_cannot_hide_gcc_plugin_behind_intercept_build(
    tmp_path: Path,
) -> None:
    intercept_build = shutil.which("intercept-build")
    compiler = shutil.which("gcc")
    if intercept_build is None or compiler is None:
        pytest.skip("intercept-build and gcc are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("gcc cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{intercept_build} {compiler} -fplugin=./payload.so "
                    "-x c -c /dev/null -o /tmp/code_fix_intercept_probe.o"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX clang-cl semantics differ on Windows")
def test_code_fix_cannot_load_clang_cl_plugin(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("clang-cl")
    if compiler is None:
        pytest.skip("clang-cl is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{compiler} -Xclang -load -Xclang ./payload.so "
                    "/c /dev/null /Fo/tmp/code_fix_clang_cl_probe.obj"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX linker plugin semantics differ on Windows")
def test_code_fix_cannot_load_linker_plugin(
    tmp_path: Path,
) -> None:
    linker = shutil.which("ld")
    compiler = shutil.which("cc") or shutil.which("gcc")
    if linker is None or compiler is None:
        pytest.skip("ld and a C compiler are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("the local C compiler cannot build a shared object")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{linker} -plugin ./payload.so -o /tmp/code_fix_ld_probe.o /dev/null",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX binutils plugin semantics differ on Windows")
def test_code_fix_cannot_load_nm_plugin(
    tmp_path: Path,
) -> None:
    nm = shutil.which("nm")
    compiler = shutil.which("cc") or shutil.which("gcc")
    if nm is None or compiler is None:
        pytest.skip("nm and a C compiler are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "object.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    built_plugin = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", "payload.so", "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    built_object = subprocess.run(
        [compiler, "-c", "object.c", "-o", "object.o"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built_plugin.returncode != 0 or built_object.returncode != 0:
        pytest.skip("the local compiler cannot build binutils fixtures")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{nm} --plugin ./payload.so object.o",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Python executable semantics differ on Windows")
def test_code_fix_cannot_use_version_qualified_python_script(
    tmp_path: Path,
) -> None:
    python = shutil.which("python3.11")
    if python is None:
        pytest.skip("python3.11 is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f'{python} -c "open(\'app.py\', \'w\').write(\'bad\')"',
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics differ on Windows")
def test_code_fix_cannot_hide_python_behind_symlinked_executable(
    tmp_path: Path,
) -> None:
    python = shutil.which("python3.11")
    if python is None:
        pytest.skip("python3.11 is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    runner = tmp_path / "runner"
    runner.symlink_to(python)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="./runner -c \"open('app.py', 'w').write('bad\\n')\"",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable semantics differ on Windows")
def test_code_fix_cannot_hide_copied_python_behind_unknown_executable(
    tmp_path: Path,
) -> None:
    python = shutil.which("python3.11")
    if python is None:
        pytest.skip("python3.11 is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    runner = tmp_path / "runner"
    runner.write_bytes(Path(python).read_bytes())
    runner.chmod(0o755)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="./runner -c \"open('app.py', 'w').write('bad\\n')\"",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable semantics differ on Windows")
def test_code_fix_cannot_run_unknown_local_script_runner(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    runner = tmp_path / "runner"
    runner.write_text("#!/bin/sh\nprintf 'bad\\n' > app.py\n", encoding="utf-8")
    runner.chmod(0o755)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="./runner")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics differ on Windows")
def test_code_fix_cannot_hide_shell_script_behind_symlinked_executable(
    tmp_path: Path,
) -> None:
    shell = Path("/bin/sh")
    if not shell.is_file():
        pytest.skip("/bin/sh is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    runner = tmp_path / "runner"
    runner.symlink_to(shell)
    (tmp_path / "payload.sh").write_text(
        "printf 'bad\\n' > app.py\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="./runner payload.sh",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics differ on Windows")
def test_code_fix_cannot_hide_find_mutation_behind_symlinked_executable(
    tmp_path: Path,
) -> None:
    find = shutil.which("find")
    if find is None:
        pytest.skip("find is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    runner = tmp_path / "runner"
    runner.symlink_to(find)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="./runner . -name fixture.py -delete",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Python executable semantics differ on Windows")
def test_code_fix_cannot_use_attached_version_qualified_python_code(
    tmp_path: Path,
) -> None:
    python = shutil.which("python3.11")
    if python is None:
        pytest.skip("python3.11 is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f'{python} -c"open(\'app.py\', \'w\').write(\'bad\\n\')"'
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Python executable semantics differ on Windows")
def test_code_fix_cannot_use_attached_version_qualified_python_module(
    tmp_path: Path,
) -> None:
    python = shutil.which("python3.11")
    if python is None:
        pytest.skip("python3.11 is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.py").write_text(
        "open('app.py', 'w').write('bad\\n')\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{python} -mpayload",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX env semantics differ on Windows")
def test_code_fix_cannot_hide_python_module_behind_env_unset(
    tmp_path: Path,
) -> None:
    python = shutil.which("python3.11")
    env = shutil.which("env")
    if python is None or env is None:
        pytest.skip("python3.11 and env are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.py").write_text(
        "open('app.py', 'w').write('bad\\n')\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{env} -u CODE_FIX_UNUSED {python} -mpayload",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX env semantics differ on Windows")
def test_code_fix_cannot_hide_python_module_behind_env_argv0(
    tmp_path: Path,
) -> None:
    python = shutil.which("python3.11")
    env = shutil.which("env")
    if python is None or env is None:
        pytest.skip("python3.11 and env are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.py").write_text(
        "open('app.py', 'w').write('bad\\n')\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{env} --argv0 CODE_FIX_ARGV0 {python} -mpayload",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell builtins differ on Windows")
def test_code_fix_cannot_hide_python_module_behind_exec_argv0(
    tmp_path: Path,
) -> None:
    python = shutil.which("python3.11")
    if python is None:
        pytest.skip("python3.11 is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.py").write_text(
        "open('app.py', 'w').write('bad\\n')\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"exec -a CODE_FIX_ARGV0 {python} -mpayload",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX sudo semantics differ on Windows")
def test_code_fix_cannot_hide_python_module_behind_sudo_user_option(
    tmp_path: Path,
) -> None:
    python = shutil.which("python3.11")
    sudo = shutil.which("sudo")
    if python is None or sudo is None:
        pytest.skip("python3.11 and sudo are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.py").write_text(
        "open('app.py', 'w').write('bad\\n')\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{sudo} -n -u ir6 {python} -mpayload",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Node executable semantics differ on Windows")
def test_code_fix_cannot_preload_node_code_into_allowlisted_command(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    payload = tmp_path / "payload.js"
    payload.write_text(
        "require('fs').writeFileSync('app.py', 'bad\\n')\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{node} --require=./payload.js --check",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Node executable semantics differ on Windows")
def test_code_fix_cannot_load_node_options_from_env_file(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.js").write_text(
        "require('fs').writeFileSync('app.py', 'bad\\n')\n",
        encoding="utf-8",
    )
    (tmp_path / "payload.env").write_text(
        "NODE_OPTIONS=--require=./payload.js\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{node} --env-file=payload.env --check",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Node executable semantics differ on Windows")
def test_code_fix_cannot_load_node_options_from_config_file(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.js").write_text(
        "require('fs').writeFileSync('app.py', 'bad\\n')\n",
        encoding="utf-8",
    )
    (tmp_path / "node.config.json").write_text(
        '{"nodeOptions":{"require":["./payload.js"]}}\n',
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{node} --experimental-config-file=node.config.json --check",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Node executable semantics differ on Windows")
def test_code_fix_cannot_load_node_experimental_loader(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.mjs").write_text(
        "import { writeFileSync } from 'node:fs';\n"
        "writeFileSync('app.py', 'bad\\n');\n"
        "export async function resolve(specifier, context, nextResolve) {\n"
        "  return nextResolve(specifier, context);\n"
        "}\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{node} --experimental-loader=./payload.mjs --input-type=module",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Node executable semantics differ on Windows")
def test_code_fix_cannot_restore_node_snapshot_blob(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    snapshot_source = tmp_path / "snapshot-source.js"
    snapshot_source.write_text(
        (
            "require('v8').startupSnapshot.addDeserializeCallback(() => "
            "require('fs').writeFileSync('app.py', 'bad\\n'))\n"
        ),
        encoding="utf-8",
    )
    built = subprocess.run(
        [node, "--build-snapshot", snapshot_source.name],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    snapshot = tmp_path / "snapshot.blob"
    if built.returncode != 0 or not snapshot.is_file():
        pytest.skip("node snapshot building is unavailable")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{node} --snapshot-blob={snapshot.name}",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)

@pytest.mark.skipif(os.name == "nt", reason="POSIX Perl executable semantics differ on Windows")
def test_code_fix_cannot_preload_perl_debugger_module(
    tmp_path: Path,
) -> None:
    perl = shutil.which("perl")
    if perl is None:
        pytest.skip("perl is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    payload = tmp_path / "Devel" / "payload.pm"
    payload.parent.mkdir()
    payload.write_text(
        "package Devel::payload; BEGIN { open my $fh, '>', 'app.py'; print $fh 'bad\\n'; close $fh } 1;\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{perl} -I. -d:payload",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Perl executable semantics differ on Windows")
@pytest.mark.parametrize("flag", ["-pe", "-ne", "-e", "-E"])
def test_code_fix_cannot_use_attached_perl_one_liner(
    tmp_path: Path,
    flag: str,
) -> None:
    perl = shutil.which("perl")
    if perl is None:
        pytest.skip("perl is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{perl} {flag}'BEGIN {{ open(F, '>', 'app.py'); "
                    "print F 'bad\\n'; close F }}'"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Node executable semantics differ on Windows")
def test_code_fix_cannot_use_attached_node_eval_script(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f'{node} --eval="require(\'fs\').writeFileSync(\'app.py\', \'bad\\n\')"'
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Node executable semantics differ on Windows")
def test_code_fix_cannot_run_node_package_script(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "package.json").write_text(
        '{"scripts":{"wipe":"truncate -s 0 app.py"}}\n',
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command=f"{node} --run=wipe")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Perl executable semantics differ on Windows")
def test_code_fix_cannot_preload_perl_module(tmp_path: Path) -> None:
    perl = shutil.which("perl")
    if perl is None:
        pytest.skip("perl is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.pm").write_text(
        "BEGIN { open my $fh, '>', 'app.py'; print $fh 'bad\\n'; close $fh; }\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command=f"{perl} -I. -Mpayload -")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Lua executable semantics differ on Windows")
@pytest.mark.parametrize("interpreter", ["lua", "luajit"])
@pytest.mark.parametrize("load_flag", ["-lpayload", "-l payload"])
def test_code_fix_cannot_preload_lua_module(
    tmp_path: Path, interpreter: str, load_flag: str
) -> None:
    executable = shutil.which(interpreter)
    if executable is None:
        pytest.skip(f"{interpreter} is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.lua").write_text(
        "local f = io.open('app.py', 'w'); f:write('bad\\n'); f:close()\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"{executable} {load_flag}",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Lua startup semantics differ on Windows")
def test_code_fix_cannot_use_versioned_lua_startup_hook(tmp_path: Path) -> None:
    lua = shutil.which("lua")
    if lua is None:
        pytest.skip("lua is not installed")
    version = subprocess.run(
        [lua, "-v"],
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"Lua (\d+)\.(\d+)", version.stdout + version.stderr)
    if match is None:
        pytest.skip("the local lua version is not discoverable")
    startup_var = f"LUA_INIT_{match.group(1)}_{match.group(2)}"
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{startup_var}='local f=io.open(\"app.py\",\"w\"); "
                    "f:write(\"bad\\n\"); f:close()' lua"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Lua executable semantics differ on Windows")
@pytest.mark.parametrize("interpreter", ["lua", "luajit"])
def test_code_fix_cannot_use_attached_lua_one_liner(
    tmp_path: Path, interpreter: str
) -> None:
    executable = shutil.which(interpreter)
    if executable is None:
        pytest.skip(f"{interpreter} is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=(
                    f"{executable} -e'local f=io.open(\"app.py\",\"w\"); "
                    "f:write(\"bad\\n\"); f:close()'"
                ),
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX glibc loader semantics differ on Windows")
def test_code_fix_cannot_load_gconv_module_through_environment(
    tmp_path: Path,
) -> None:
    compiler = shutil.which("cc") or shutil.which("gcc")
    iconv = shutil.which("iconv")
    if compiler is None or iconv is None:
        pytest.skip("a C compiler and iconv are required")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    gconv = tmp_path / "gconv"
    gconv.mkdir()
    (gconv / "gconv-modules").write_text(
        "module PWN// UTF-8// payload 1\n",
        encoding="utf-8",
    )
    (tmp_path / "payload.c").write_text(
        "#include <stdio.h>\n"
        "__attribute__((constructor)) static void inject(void) {\n"
        "    FILE *fh = fopen(\"app.py\", \"w\");\n"
        "    if (fh) { fputs(\"bad\\n\", fh); fclose(fh); }\n"
        "}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", str(gconv / "payload.so"), "payload.c"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip("the local compiler cannot build gconv module")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"GCONV_PATH=./gconv {iconv} -f PWN -t UTF-8",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX environment semantics differ on Windows")
def test_code_fix_cannot_inject_python_sitecustomize_into_pytest(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "sitecustomize.py").write_text(
        "from pathlib import Path\nPath('app.py').write_text('bad')\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="PYTHONPATH=payload python3 -m pytest -q",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX environment semantics differ on Windows")
def test_code_fix_cannot_inject_pytest_plugin_through_addopts(
    tmp_path: Path,
) -> None:
    python = shutil.which("python3")
    if python is None:
        pytest.skip("python3 is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload_plugin.py").write_text(
        "from pathlib import Path\nPath('app.py').write_text('bad\\n')\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"PYTEST_ADDOPTS='-p payload_plugin' {python} -m pytest -q",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pytest semantics differ on Windows")
def test_code_fix_cannot_inject_pytest_plugin_through_option(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload_plugin.py").write_text(
        "from pathlib import Path\nPath('app.py').write_text('bad\\n')\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="python3 -m pytest -p payload_plugin -q",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pytest semantics differ on Windows")
def test_code_fix_cannot_import_arbitrary_package_through_pyargs(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    package = tmp_path / "payload_pkg"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from pathlib import Path\nPath('app.py').write_text('bad\\n')\n",
        encoding="utf-8",
    )
    (package / "test_payload.py").write_text(
        "def test_payload():\n    assert True\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="python3 -m pytest --pyargs payload_pkg.test_payload -q",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pytest semantics differ on Windows")
def test_code_fix_cannot_inject_pytest_plugin_through_override_ini(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload_plugin.py").write_text(
        "from pathlib import Path\nPath('app.py').write_text('bad\\n')\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="python3 -m pytest -o 'addopts=-p payload_plugin' -q",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pytest semantics differ on Windows")
def test_code_fix_cannot_import_package_through_doctest_modules(
    tmp_path: Path,
) -> None:
    package = tmp_path / "payload_pkg"
    package.mkdir()
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (package / "__init__.py").write_text(
        "from pathlib import Path\nPath('app.py').write_text('bad\\n')\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="python3 -m pytest --doctest-modules payload_pkg -q",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pytest semantics differ on Windows")
def test_code_fix_cannot_execute_pytest_doctest_glob(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload.txt").write_text(
        ">>> open(\"app.py\", \"w\").write(\"bad\\n\")\n8\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="pytest --doctest-glob=payload.txt -q",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pytest semantics differ on Windows")
def test_code_fix_cannot_load_pytest_debugger_module(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    (tmp_path / "payload_debug.py").write_text(
        "from pathlib import Path\nPath('app.py').write_text('bad\\n')\n"
        "class Debugger: pass\n",
        encoding="utf-8",
    )
    (target.parent / "test_failure.py").write_text(
        "def test_failure():\n    assert False\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="python3 -m pytest --pdb --pdbcls=payload_debug:Debugger -q",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pytest semantics differ on Windows")
def test_code_fix_cannot_hide_pytest_imports_in_config_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    package = tmp_path / "payload_pkg"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from pathlib import Path\nPath('app.py').write_text('bad\\n')\n",
        encoding="utf-8",
    )
    (package / "test_payload.py").write_text(
        "def test_payload():\n    assert True\n",
        encoding="utf-8",
    )
    (tmp_path / "payload.ini").write_text(
        "[pytest]\npythonpath = .\naddopts = --pyargs payload_pkg.test_payload\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="pytest -c payload.ini -q")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pytest semantics differ on Windows")
def test_code_fix_cannot_hide_pytest_config_behind_rootdir(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    root = tmp_path / "payload_root"
    package = root / "payload_pkg"
    package.mkdir(parents=True)
    (root / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\naddopts = --pyargs payload_pkg.test_payload\n",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        "from pathlib import Path\nPath('app.py').write_text('bad\\n')\n",
        encoding="utf-8",
    )
    (package / "test_payload.py").write_text(
        "def test_payload():\n    assert True\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="pytest --rootdir=payload_root -q")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pytest semantics differ on Windows")
@pytest.mark.parametrize(
    "option",
    [
        "--debug=tests/fixture.py",
        "--log-file tests/fixture.py",
        "--log-file=tests/fixture.py",
    ],
)
def test_code_fix_cannot_write_pytest_log_into_evaluator(
    tmp_path: Path,
    option: str,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"python3 -m pytest {option} -q",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics differ on Windows")
def test_code_fix_cannot_hide_git_mutation_behind_symlinked_executable(
    tmp_path: Path,
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    subprocess.run([git, "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        [git, "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        [git, "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run([git, "add", "tests/fixture.py"], cwd=tmp_path, check=True)
    subprocess.run([git, "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    target.write_text("value = 999\n", encoding="utf-8")
    runner = tmp_path / "runner"
    runner.symlink_to(git)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="./runner checkout -- .",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 999\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX environment semantics differ on Windows")
def test_code_fix_cannot_inject_git_external_diff_hook(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    source = tmp_path / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    hook = tmp_path / "payload.sh"
    hook.write_text("truncate -s 0 app.py\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "source.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    source.write_text("value = 2\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="GIT_EXTERNAL_DIFF=./payload.sh git diff",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX environment semantics differ on Windows")
@pytest.mark.parametrize(
    "command",
    [
        (
            "GIT_CONFIG_COUNT=1 "
            "GIT_CONFIG_KEY_0=core.fsmonitor "
            "GIT_CONFIG_VALUE_0=./payload.sh git status"
        ),
        (
            "sh -c 'env GIT_CONFIG_COUNT=1 "
            "GIT_CONFIG_KEY_0=core.fsmonitor "
            "GIT_CONFIG_VALUE_0=./payload.sh git status'"
        ),
    ],
)
def test_code_fix_cannot_inject_indexed_git_config_hook(
    tmp_path: Path, command: str
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    source = tmp_path / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    hook = tmp_path / "payload.sh"
    hook.write_text("truncate -s 0 app.py\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "source.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    source.write_text("value = 2\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=command,
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX environment semantics differ on Windows")
def test_code_fix_cannot_enable_git_external_diff_option(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    source = tmp_path / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    hook = tmp_path / "payload.sh"
    hook.write_text("truncate -s 0 app.py\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "source.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    source.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "config", "diff.external", "./payload.sh"], cwd=tmp_path, check=True)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="git diff --ext-diff")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX environment semantics differ on Windows")
@pytest.mark.parametrize(
    "command",
    [
        "git diff",
        "git diff --textconv",
        "git show --textconv HEAD:source.txt",
        "git log -p --textconv -1",
    ],
)
@pytest.mark.parametrize("driver_setting", ["textconv", "command"])
def test_code_fix_cannot_enable_git_textconv_option(
    tmp_path: Path, command: str, driver_setting: str
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    source = tmp_path / "source.txt"
    source.write_text("value = 1\n", encoding="utf-8")
    attributes = tmp_path / ".gitattributes"
    attributes.write_text("source.txt diff=payload\n", encoding="utf-8")
    hook = tmp_path / "payload.sh"
    hook.write_text("truncate -s 0 app.py\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", f"diff.payload.{driver_setting}", "./payload.sh"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "add", "source.txt", ".gitattributes"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    source.write_text("value = 2\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command=command)])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX submodule semantics differ on Windows")
def test_code_fix_cannot_enable_git_submodule_external_diff(
    tmp_path: Path,
) -> None:
    module_source = tmp_path / "module-source"
    module_source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=module_source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=module_source, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=module_source, check=True)
    (module_source / "tests").mkdir()
    (module_source / "tests" / "fixture.py").write_text("value = 200\n", encoding="utf-8")
    (module_source / "source.txt").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=module_source, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=module_source, check=True)

    workspace = tmp_path / "super"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(module_source), "module"],
        cwd=workspace,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(["git", "commit", "-qm", "add module"], cwd=workspace, check=True)
    target = workspace / "module" / "tests" / "fixture.py"
    alias = workspace / "module" / "app.py"
    alias.hardlink_to(target)
    source = workspace / "module" / "source.txt"
    source.write_text("value = 2\n", encoding="utf-8")
    hook = workspace / "module" / "payload.sh"
    hook.write_text("truncate -s 0 app.py\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "config", "diff.external", "./payload.sh"], cwd=workspace / "module", check=True)
    state = make_state(workspace)
    state.source_snapshot = result_progress.snapshot_sources(workspace)
    llm = StubLlm(replies=[action("run_shell", command="git diff --submodule=diff")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX worktree semantics differ on Windows")
def test_code_fix_cannot_run_textconv_from_linked_worktree_common_config(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    (repository / "tests").mkdir()
    (repository / "tests" / "fixture.py").write_text("value = 200\n", encoding="utf-8")
    (repository / "source.txt").write_text("value = 1\n", encoding="utf-8")
    (repository / ".gitattributes").write_text("source.txt diff=payload\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
    linked = tmp_path / "linked"
    subprocess.run(["git", "worktree", "add", "-q", str(linked), "HEAD"], cwd=repository, check=True)
    subprocess.run(["git", "config", "diff.payload.textconv", "./payload.sh"], cwd=repository, check=True)
    target = linked / "tests" / "fixture.py"
    alias = linked / "app.py"
    alias.hardlink_to(target)
    source = linked / "source.txt"
    source.write_text("value = 2\n", encoding="utf-8")
    hook = linked / "payload.sh"
    hook.write_text("truncate -s 0 app.py\n", encoding="utf-8")
    hook.chmod(0o755)
    state = make_state(linked)
    state.source_snapshot = result_progress.snapshot_sources(linked)
    llm = StubLlm(replies=[action("run_shell", command="git diff")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Git config semantics differ on Windows")
def test_code_fix_cannot_follow_git_include_to_textconv(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "tests").mkdir()
    target = tmp_path / "tests" / "fixture.py"
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    source = tmp_path / "source.txt"
    source.write_text("value = 1\n", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text("source.txt diff=payload\n", encoding="utf-8")
    hook = tmp_path / "payload.sh"
    hook.write_text("truncate -s 0 app.py\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    included = tmp_path / ".git" / "included.conf"
    included.write_text(
        '[diff "payload"]\n\ttextconv = ./payload.sh\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "config", "include.path", "included.conf"], cwd=tmp_path, check=True)
    source.write_text("value = 2\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="git diff")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Git filter semantics differ on Windows")
def test_code_fix_cannot_run_git_filter_command(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "tests").mkdir()
    target = tmp_path / "tests" / "fixture.py"
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    source = tmp_path / "source.txt"
    source.write_text("value = 1\n", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text("source.txt filter=payload\n", encoding="utf-8")
    hook = tmp_path / "payload.sh"
    hook.write_text("truncate -s 0 app.py\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "filter.payload.clean", "./payload.sh"], cwd=tmp_path, check=True)
    source.write_text("value = 2\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="git diff")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Git hook semantics differ on Windows")
def test_code_fix_cannot_run_git_fsmonitor_hook(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "tests").mkdir()
    target = tmp_path / "tests" / "fixture.py"
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    source = tmp_path / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    hook = tmp_path / "payload.sh"
    hook.write_text("truncate -s 0 app.py\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "add", "source.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "extensions.worktreeConfig", "true"], cwd=tmp_path, check=True)
    (tmp_path / ".git" / "config.worktree").write_text(
        "[core]\n\tfsmonitor = ./payload.sh\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="git status")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="POSIX Git config semantics differ on Windows")
def test_code_fix_cannot_select_global_git_config_with_home(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "tests").mkdir()
    target = tmp_path / "tests" / "fixture.py"
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    source = tmp_path / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    hook = tmp_path / "payload.sh"
    hook.write_text("truncate -s 0 app.py\n", encoding="utf-8")
    hook.chmod(0o755)
    home = tmp_path / "git-home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        "[core]\n\tfsmonitor = ./payload.sh\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "source.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="HOME=git-home git status")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="hardlink semantics differ on Windows")
def test_code_fix_cannot_mutate_evaluator_through_hardlink_path_argument(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    alias = tmp_path / "app.py"
    alias.hardlink_to(target)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="truncate -s 0 app.py",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_edit_git_control_metadata(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    config = tmp_path / ".git" / "config"
    original = config.read_bytes()
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "write_file",
                path=str(config),
                content="[core]\n\tfsmonitor = ./payload.sh\n",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert config.read_bytes() == original
    assert outcome.solved is False
    assert any("repository control" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_edit_git_control_through_hardlink(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    config = tmp_path / ".git" / "config"
    alias = tmp_path / "repo-config-alias"
    alias.hardlink_to(config)
    original = config.read_bytes()
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "write_file",
                path=str(alias),
                content="[core]\n\tfsmonitor = ./payload.sh\n",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert config.read_bytes() == original
    assert outcome.solved is False
    assert any("repository control" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
def test_code_fix_cannot_redirect_through_symlinked_git_directory(
    tmp_path: Path,
) -> None:
    gitdir = tmp_path / "external" / "gitdir"
    gitdir.mkdir(parents=True)
    config = gitdir / "config"
    config.write_text("[core]\n", encoding="utf-8")
    (tmp_path / ".git").symlink_to(gitdir, target_is_directory=True)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="printf '[core]\\nfsmonitor = ./payload.sh\\n' > .git/config",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert config.read_text(encoding="utf-8") == "[core]\n"
    assert outcome.solved is False
    assert any("repository control" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
@pytest.mark.parametrize("tool", ["write_file", "replace_text"])
def test_code_fix_cannot_edit_symlinked_git_directory(
    tmp_path: Path, tool: str
) -> None:
    gitdir = tmp_path / "external" / "gitdir"
    gitdir.mkdir(parents=True)
    config = gitdir / "config"
    config.write_text("[core]\n", encoding="utf-8")
    (tmp_path / ".git").symlink_to(gitdir, target_is_directory=True)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    if tool == "write_file":
        reply = action(
            tool,
            path=str(tmp_path / ".git" / "config"),
            content="[core]\nfsmonitor = ./payload.sh\n",
        )
    else:
        reply = action(
            tool,
            path=str(tmp_path / ".git" / "config"),
            old="[core]",
            new="[core]\nfsmonitor = ./payload.sh",
        )
    outcome = run(state, StubLlm(replies=[reply]), FakeVerify(), playbook=playbooks.CODE_FIX)

    assert config.read_text(encoding="utf-8") == "[core]\n"
    assert outcome.solved is False
    assert any("repository control" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_use_ruff_format_on_evaluator_tests(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command=f"ruff format '{target}'")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_send_pytest_temp_files_into_evaluator_tests(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[action("run_shell", command="pytest --basetemp=tests -q")]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_send_pytest_report_into_evaluator_tests(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"pytest --junitxml='{target}' app.py",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_allows_read_only_pytest_on_xml_named_test(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_xml.py"
    target.parent.mkdir()
    target.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(replies=[action("run_shell", command=f"pytest -q '{target}'")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "def test_ok():\n    assert True\n"
    assert not any("code-fix refused" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_send_git_diff_output_into_evaluator_tests(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"git diff -o'{target}' -- app.py",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_git_checkout_to_mutate_workspace(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="git checkout -- .")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_env_git_checkout_to_mutate_workspace(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=tmp_path,
        check=True,
    )
    target.write_text("assert status == 400\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="env git checkout -- .")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 400\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_git_assignment_to_mutate_workspace(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=tmp_path,
        check=True,
    )
    target.write_text("assert status == 400\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[action("run_shell", command="GIT_OPTIONAL_LOCKS=0 git checkout -- .")]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 400\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_compound_git_checkout_to_mutate_workspace(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=tmp_path,
        check=True,
    )
    target.write_text("assert status == 400\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="true; git checkout -- .")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 400\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_command_substitution_git_checkout(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=tmp_path,
        check=True,
    )
    target.write_text("assert status == 400\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="echo $(git checkout -- .)")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 400\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_newline_git_checkout_to_mutate_workspace(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=tmp_path,
        check=True,
    )
    target.write_text("assert status == 400\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="true\n git checkout -- .")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 400\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_command_substitution_find_delete(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="echo $(find . -delete)",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_subshell_git_checkout(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=tmp_path,
        check=True,
    )
    target.write_text("assert status == 400\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="(git checkout -- .)")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 400\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_brace_git_checkout(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=tmp_path,
        check=True,
    )
    target.write_text("assert status == 400\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="{ git checkout -- .; }")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 400\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_conditional_git_checkout(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=tmp_path,
        check=True,
    )
    target.write_text("assert status == 400\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[action("run_shell", command="if true; then git checkout -- .; fi")]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 400\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_use_find_xargs_to_mutate_evaluator_tests(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="find . -type f -print0 | xargs -0 rm -f",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_use_find_while_to_mutate_evaluator_tests(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="find . -type f -print | while IFS= read -r f; do rm -f \"$f\"; done",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_delete_workspace_root(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="rm -rf .")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_delete_pwd_workspace_root(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command='rm -rf "$PWD/."')])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_extract_archive_into_workspace_root(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    payload = tmp_path / "payload"
    (payload / "tests").mkdir(parents=True)
    (payload / "tests" / "fixture.py").write_text("value = 900\n", encoding="utf-8")
    archive = tmp_path / "payload.tar"
    subprocess.run(
        ["tar", "-cf", str(archive), "-C", str(payload), "tests/fixture.py"], check=True
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="tar -xf payload.tar -C .")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_extract_archive_with_attached_root(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    payload = tmp_path / "payload"
    (payload / "tests").mkdir(parents=True)
    (payload / "tests" / "fixture.py").write_text("value = 900\n", encoding="utf-8")
    archive = tmp_path / "payload.tar"
    subprocess.run(
        ["tar", "-cf", str(archive), "-C", str(payload), "tests/fixture.py"], check=True
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[action("run_shell", command="tar -xf payload.tar --directory=.")]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_extract_archive_through_env(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    payload = tmp_path / "payload"
    (payload / "tests").mkdir(parents=True)
    (payload / "tests" / "fixture.py").write_text("value = 900\n", encoding="utf-8")
    archive = tmp_path / "payload.tar"
    subprocess.run(
        ["tar", "-cf", str(archive), "-C", str(payload), "tests/fixture.py"], check=True
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="env tar -xf payload.tar -C .")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_extract_archive_through_shell_wrapper(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    payload = tmp_path / "payload"
    (payload / "tests").mkdir(parents=True)
    (payload / "tests" / "fixture.py").write_text("value = 900\n", encoding="utf-8")
    archive = tmp_path / "payload.tar"
    subprocess.run(
        ["tar", "-cf", str(archive), "-C", str(payload), "tests/fixture.py"], check=True
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[action("run_shell", command="sh -c 'tar -xf payload.tar -C .'")]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_shell_wrapper_find_delete(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="sh -c 'find . -delete'")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_use_shell_wrapper_find_while(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="sh -c 'find . -type f -print | while IFS= read -r f; do rm -f \"$f\"; done'",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_shell_option_find_delete(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="sh -e -c 'find . -delete'")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_eval_to_mutate_evaluator_tests(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="eval 'find . -delete'")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_extract_archive_into_implicit_workspace(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    payload = tmp_path / "payload"
    (payload / "tests").mkdir(parents=True)
    (payload / "tests" / "fixture.py").write_text("value = 900\n", encoding="utf-8")
    archive = tmp_path / "payload.tar"
    subprocess.run(
        ["tar", "-cf", str(archive), "-C", str(payload), "tests/fixture.py"], check=True
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="tar -xf payload.tar")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_extract_archive_with_tar_get(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    payload = tmp_path / "payload"
    (payload / "tests").mkdir(parents=True)
    (payload / "tests" / "fixture.py").write_text("value = 900\n", encoding="utf-8")
    archive = tmp_path / "payload.tar"
    subprocess.run(
        ["tar", "-cf", str(archive), "-C", str(payload), "tests/fixture.py"], check=True
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="tar --get -f payload.tar")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_python_script_to_mutate_workspace(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="python3 -c 'import shutil; shutil.rmtree(\".\")'",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_python_zipfile_to_mutate_workspace(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    payload = tmp_path / "payload"
    (payload / "tests").mkdir(parents=True)
    (payload / "tests" / "fixture.py").write_text("value = 900\n", encoding="utf-8")
    archive = tmp_path / "payload.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(payload / "tests" / "fixture.py", "tests/fixture.py")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[action("run_shell", command="python3 -m zipfile -e payload.zip .")]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_execute_mutating_shell_script(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    script = tmp_path / "payload.sh"
    script.write_text("rm -f tests/fixture.py\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="sh payload.sh")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_use_perl_script_to_mutate_workspace(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="perl -MFile::Find -e 'find(sub { unlink $_ if -f $_ }, q(.))'",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_use_awk_system_to_mutate_workspace(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="awk 'BEGIN { system(\"find . -type f -delete\") }'",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_use_lua_script_to_mutate_workspace(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="lua -e 'os.execute(\"find . -type f -delete\")'",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_rsync_into_workspace_root(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    payload = tmp_path / "payload" / "tests"
    payload.mkdir(parents=True)
    (payload / "fixture.py").write_text("value = 900\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="rsync -a payload/ .")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_apply_hidden_patch_payload(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    patch_file = tmp_path / "payload.patch"
    patch_file.write_text(
        "--- tests/fixture.py\n+++ tests/fixture.py\n@@ -1 +1 @@\n-value = 200\n+value = 900\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="patch -i payload.patch")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_execute_make_recipe_payload(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    makefile = tmp_path / "payload.mk"
    makefile.write_text("wipe:\n\trm -f tests/fixture.py\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="make -f payload.mk wipe")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_execute_cmake_script_payload(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    script = tmp_path / "payload.cmake"
    script.write_text('file(REMOVE "tests/fixture.py")\n', encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="cmake -P payload.cmake")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_execute_npm_script_payload(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    package = tmp_path / "package.json"
    package.write_text(
        '{"scripts":{"wipe":"rm -f tests/fixture.py"}}\n', encoding="utf-8"
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="npm run wipe")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_install_malicious_package_payload(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    package = tmp_path / "payload"
    package.mkdir()
    (package / "setup.py").write_text(
        "from pathlib import Path\n"
        "Path('tests/fixture.py').unlink()\n"
        "from setuptools import setup\n"
        "setup(name='payload', version='0.0.1')\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command="pip install ./payload --no-deps --no-build-isolation --target install",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_run_malicious_go_payload(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    payload = tmp_path / "payload.go"
    payload.write_text(
        'package main\nimport "os"\nfunc main() { _ = os.Remove("tests/fixture.py") }\n',
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="go run payload.go")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_change_workspace_permissions(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    original_mode = target.stat().st_mode & 0o777
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="chmod -R 000 .")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.stat().st_mode & 0o777 == original_mode
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_extract_7z_into_workspace(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    payload = tmp_path / "payload" / "tests"
    payload.mkdir(parents=True)
    (payload / "fixture.py").write_text("value = 900\n", encoding="utf-8")
    archive = tmp_path / "payload.7z"
    subprocess.run(["7z", "a", "-bd", str(archive), str(payload / "fixture.py")], check=True)
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="7z x payload.7z")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(shutil.which("cpio") is None, reason="cpio is unavailable")
def test_code_fix_cannot_extract_cpio_into_workspace(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    payload = tmp_path / "payload"
    (payload / "tests").mkdir(parents=True)
    (payload / "tests" / "fixture.py").write_text("value = 900\n", encoding="utf-8")
    archive = tmp_path / "payload.cpio"
    subprocess.run(
        ["cpio", "-o", "-H", "newc", "-F", str(archive)],
        cwd=payload,
        input=b"tests/fixture.py\n",
        stdout=subprocess.PIPE,
        check=True,
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="cpio -id -F payload.cpio")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_run_go_generate_directive(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    subprocess.run(["go", "mod", "init", "example.invalid/payload"], cwd=tmp_path, check=True)
    (tmp_path / "payload.go").write_text(
        "package payload\n//go:generate rm -f tests/fixture.py\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="go generate ./...")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_use_curl_config_to_overwrite_evaluator(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    payload = tmp_path / "payload.txt"
    payload.write_text("value = 900\n", encoding="utf-8")
    config = tmp_path / "payload.conf"
    config.write_text(f"url = file://{payload}\noutput = {target}\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="curl -K payload.conf")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_copy_scp_payload_into_workspace(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    payload = tmp_path / "payload" / "tests"
    payload.mkdir(parents=True)
    (payload / "fixture.py").write_text("value = 900\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="scp -r payload/. .")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_run_container_with_workspace_mount(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command='docker run --rm -v "$PWD:/workspace" alpine',
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_delete_pwd_parent_workspace_alias(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f'rm -rf "$PWD/../{tmp_path.name}"',
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(shutil.which("vim") is None, reason="vim is unavailable")
def test_code_fix_cannot_execute_vim_script_payload(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    script = tmp_path / "payload.vim"
    script.write_text("call delete('tests/fixture.py')\nqa!\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="vim -es -S payload.vim")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(shutil.which("gdb") is None, reason="gdb is unavailable")
def test_code_fix_cannot_execute_gdb_command_payload(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    script = tmp_path / "payload.gdb"
    script.write_text("set pagination off\nshell rm -f tests/fixture.py\nquit\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="gdb -batch -x payload.gdb")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(
    shutil.which("javac") is None or shutil.which("jar") is None,
    reason="JDK is unavailable",
)
def test_code_fix_cannot_execute_java_jar_payload(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    source = tmp_path / "Payload.java"
    source.write_text(
        "import java.nio.file.*;\n"
        "public class Payload { public static void main(String[] a) throws Exception { Files.deleteIfExists(Path.of(\"tests/fixture.py\")); } }\n",
        encoding="utf-8",
    )
    classes = tmp_path / "classes"
    classes.mkdir()
    subprocess.run(["javac", "-d", str(classes), str(source)], check=True, cwd=tmp_path)
    archive = tmp_path / "payload.jar"
    subprocess.run(
        ["jar", "cfe", str(archive), "Payload", "-C", str(classes), "Payload.class"],
        check=True,
        cwd=tmp_path,
    )
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="java -jar payload.jar")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_cannot_execute_m4_payload(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "fixture.py"
    target.parent.mkdir()
    target.write_text("value = 200\n", encoding="utf-8")
    payload = tmp_path / "payload.m4"
    payload.write_text('syscmd("rm -f tests/fixture.py")\n', encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(replies=[action("run_shell", command="m4 payload.m4")])

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "value = 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_send_linter_output_into_evaluator_tests(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"flake8 --output-file '{target}' app.py",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_send_attached_linter_output_into_evaluator_tests(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"flake8 -o'{target}' app.py",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_send_mypy_report_into_evaluator_tests(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"mypy --junit-xml '{target}' app.py",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_cannot_send_mypy_linecount_report_into_evaluator_tests(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_api.py"
    target.parent.mkdir()
    target.write_text("assert status == 200\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    llm = StubLlm(
        replies=[
            action(
                "run_shell",
                command=f"mypy --linecount-report '{target}' app.py",
            )
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert target.read_text(encoding="utf-8") == "assert status == 200\n"
    assert outcome.solved is False
    assert any("evaluator" in text.lower() for text in outcome.observations)


def test_code_fix_finalises_with_changed_source_not_a_report(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    report = tmp_path / "README.md"
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    repeated = action("list_dir", path=str(tmp_path))
    llm = StubLlm(
        replies=[
            action("write_file", path=str(report), content="notes"),
            action("replace_text", path=str(target), old="answer = 1", new="answer = 2"),
        ]
        + [repeated for _ in range(limits.NO_PROGRESS_STOP + 4)]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert outcome.stop_reason == "stuck"
    assert outcome.deliverable == target


@pytest.mark.skipif(os.name == "nt", reason="the agent shell is POSIX-only")
def test_code_fix_finalises_with_source_changed_through_shell(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    report = tmp_path / "README.md"
    state = make_state(tmp_path)
    state.source_snapshot = result_progress.snapshot_sources(tmp_path)
    repeated = action("list_dir", path=str(tmp_path))
    command = f"printf 'answer = 2\\n' > '{target}'"
    llm = StubLlm(
        replies=[
            action("write_file", path=str(report), content="notes"),
            action("run_shell", command=command),
        ]
        + [repeated for _ in range(limits.NO_PROGRESS_STOP + 4)]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    assert outcome.stop_reason == "stuck"
    assert outcome.deliverable == target


def test_solved_is_true_only_when_the_run_stopped_at_a_green_check(tmp_path: Path) -> None:
    target = tmp_path / "done.txt"
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content="x"),
            action("finish", path=str(target), verify="test -s done.txt"),
        ]
    )

    outcome = run(make_state(tmp_path), llm, FakeVerify((True, "command", "")))

    assert outcome.solved is True
    assert outcome.stop_reason == "verified", "verified is the only stop that proves anything"


def test_a_red_check_is_not_overwritten_by_a_second_look_at_the_same_file(
    tmp_path: Path,
) -> None:
    # Finalisation must not re-verify a run the verifier already rejected. The second
    # call would come with no command, land on `unverified`, and leave the trace
    # ending in a pass right after a real failure -- the red detail is the one thing
    # a human reading this zero actually needs.
    target = tmp_path / "report.json"
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content="{}"),
            action("finish", path=str(target), verify="pytest", summary="v1"),
        ]
    )
    state = make_state(tmp_path)
    state.budget.recovery_passes_used = state.budget.max_recovery_passes
    verify = FakeVerify((False, "command", "exit 1: assert findings"))

    outcome = run(state, llm, verify)

    assert len(verify.calls) == 1, "the rejected deliverable is not offered a second time"
    assert outcome.verdict is not None
    assert outcome.verdict.ok is False
    assert outcome.verdict.stage == "command"
    stages = [event.name for event in state.audit if event.kind == "verify"]
    assert stages[-1] == "command", "the trace must not end on a friendlier verdict"


# --- the shapes a small model actually sends ----------------------------------


def test_arguments_nested_under_args_are_folded_up_not_dropped(tmp_path: Path) -> None:
    # Most agent frameworks use {"tool": ..., "args": {...}}, so a model trained on
    # them emits it against our flat schema. `ACTION_SCHEMA` sets no
    # additionalProperties, so this passes validation and arrives here looking valid
    # with every field empty -- a silently wrong action, which is worse than an error.
    target = tmp_path / "nested.txt"
    llm = StubLlm(
        replies=[
            LlmReply(
                text=json.dumps(
                    {"tool": "write_file", "args": {"path": str(target), "content": "folded"}}
                )
            )
        ]
    )

    run(make_state(tmp_path), llm, FakeVerify())

    assert target.read_text(encoding="utf-8") == "folded"


def test_arguments_nested_under_the_openai_name_are_folded_too(tmp_path: Path) -> None:
    target = tmp_path / "args.txt"
    llm = StubLlm(
        replies=[
            LlmReply(
                text=json.dumps(
                    {"tool": "write_file", "arguments": {"path": str(target), "content": "x"}}
                )
            )
        ]
    )

    run(make_state(tmp_path), llm, FakeVerify())

    assert target.read_text(encoding="utf-8") == "x"


def test_a_top_level_field_wins_over_the_same_field_nested(tmp_path: Path) -> None:
    target = tmp_path / "outer.txt"
    llm = StubLlm(
        replies=[
            LlmReply(
                text=json.dumps(
                    {
                        "tool": "write_file",
                        "path": str(target),
                        "content": "outer",
                        "args": {"content": "inner"},
                    }
                )
            )
        ]
    )

    run(make_state(tmp_path), llm, FakeVerify())

    assert target.read_text(encoding="utf-8") == "outer", "the schema's own shape is the truth"


def test_a_relative_path_is_resolved_against_the_workspace(tmp_path: Path) -> None:
    # The process cwd is the agent's own install directory, not the task's -- that is
    # the whole reason `resolve_workspace` exists. A relative path read against cwd
    # silently inspects the wrong tree and reports a clean "not found".
    (tmp_path / "routers").mkdir()
    (tmp_path / "routers" / "auth.py").write_text("QUERY = 'select 1'\n", encoding="utf-8")
    llm = StubLlm(replies=[action("read_range", path="routers/auth.py")])

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert any("select 1" in text for text in outcome.observations)


def test_an_action_missing_the_argument_its_tool_needs_is_an_error_not_a_no_op(
    tmp_path: Path,
) -> None:
    # `sh -c ""` exits 0 with no output: a silent no-op that reads as success and
    # teaches the model nothing.
    llm = StubLlm(replies=[action("run_shell"), action("search", path=str(tmp_path))])

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert any("run_shell needs `command`" in text for text in outcome.observations)
    assert any("search needs `pattern`" in text for text in outcome.observations)


def test_write_file_without_a_path_never_reaches_the_filesystem(tmp_path: Path) -> None:
    before = sorted(entry.name for entry in tmp_path.iterdir())
    llm = StubLlm(replies=[action("write_file", content="orphan")])

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert any("write_file needs `path`" in text for text in outcome.observations)
    assert sorted(entry.name for entry in tmp_path.iterdir()) == before


def test_write_file_without_a_content_key_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    # A reply that lost its payload after a truncated or schema-constrained turn: the
    # old behaviour wrote an empty deliverable over the report and scored 0
    # (REVIEW-public-q36). `content: ""` still writes an empty file on purpose.
    target = tmp_path / "report.json"
    llm = StubLlm(replies=[action("write_file", path=str(target)), action("write_file", path=str(target), content="")])

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert any("write_file needs `content`" in text for text in outcome.observations)
    assert target.exists() and target.read_text(encoding="utf-8") == ""


def test_the_loop_hands_the_write_policy_to_the_tool(tmp_path: Path) -> None:
    """Записанные в задаче пути должны доходить до `write_file`, а не оставаться в state.

    Ровно этот провод и есть исправление E011: workspace был вычислен как собственный
    каталог агента, задача назвала артефакт абсолютным путём, и запрет записи вне
    workspace превращал верное действие в артефакт не в том месте. Без этого теста
    провод можно удалить, и весь сьют останется зелёным — проверено мутацией.
    """
    workspace = tmp_path / "opt" / "harbor" / "local-agent"
    workspace.mkdir(parents=True)
    target = tmp_path / "srv" / "bootstrap" / "farewell.marker"
    target.parent.mkdir(parents=True)

    state = AgentState(
        instruction=f"Создай {target} с содержимым Ciao.",
        workspace=workspace,
        budget=Budget(total_s=600.0, clock=lambda: 0.0),
        named_paths=(target,),
    )
    llm = StubLlm(replies=[action("write_file", path=str(target), content="Ciao")] * 3)

    run(state, llm)

    assert target.read_bytes() == b"Ciao", "запись по названному задачей пути была отклонена"
def test_finish_pointing_at_a_directory_is_refused_by_the_gate(tmp_path: Path) -> None:
    # `_deliverable` asked only whether something exists at the path, so a directory
    # walked through the gate and was handed to the verifier, which reported it as
    # "not found" -- a wrong sentence about a path the model can see is there, and it
    # cost the single recovery pass to say it. The shape of the claim belongs to the
    # gate; only its content belongs to the verifier.
    target = tmp_path / "report"
    target.mkdir()
    llm = StubLlm(
        replies=[
            action("finish", path=str(target), verify="", summary="сделано"),
            action("write_file", path=str(tmp_path / "report.txt"), content="real"),
            action("finish", path=str(tmp_path / "report.txt"), verify="", summary="сделано"),
        ]
    )
    verify = FakeVerify((True, "existence", ""))
    state = make_state(tmp_path)

    outcome = run(state, llm, verify)

    assert outcome.solved is True, "the run recovers once a real file is named"
    assert state.budget.recovery_passes_used == 0, "a mis-shaped claim is not a failed check"
    assert [call[0] for call in verify.calls] == [tmp_path / "report.txt"], (
        "the directory never reaches the verifier"
    )
    assert any("directory" in text for text in outcome.observations)


def test_a_stuck_signal_with_nothing_on_disk_is_advice_rather_than_an_ending(
    tmp_path: Path,
) -> None:
    """E024 measured what stopping costs: four runs quit with half the budget unused.

    `fix-redirect-scheme` stopped after 67.5 s of a 180 s budget having written no file
    at all -- its trace is six `read_range` calls and nothing else. A run in that state
    has no deliverable to protect, so ending it converts a maybe into a certain zero.
    """
    repeated = action("list_dir", path=str(tmp_path))
    llm = StubLlm(replies=[repeated for _ in range(limits.NO_PROGRESS_STOP + 3)])

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert outcome.stop_reason != "stuck"
    # Which of the two warnings arrives depends on which detector sees the cycle first:
    # since E-P2d a fruitless repeat is refused before it runs, which is earlier than
    # the no-progress window fills. Either way the model is told, and the run continues.
    assert any(
        "по кругу" in text or "returned the same output" in text
        for text in outcome.observations
    )


def test_a_stuck_signal_ends_the_run_once_a_deliverable_exists(tmp_path: Path) -> None:
    """The other half of E028, and the more expensive one.

    `mut-salutation-beacon` and `ctf-header-override` both wrote a deliverable their
    verifier was already satisfied with, then circled. E024 stopped them there and
    scored 1 twice; E028/E029 let them run on and each spent its remaining steps
    rewriting the file it had already got right, twice scoring 0. Circling with
    something to lose is the one case where stopping is the cheaper move.
    """
    target = tmp_path / "answer.txt"
    repeated = action("list_dir", path=str(tmp_path))
    llm = StubLlm(
        replies=[action("write_file", path=str(target), content="earned")]
        + [repeated for _ in range(limits.NO_PROGRESS_STOP + 3)]
    )
    state = make_state(tmp_path)

    outcome = run(state, llm, FakeVerify())

    assert outcome.stop_reason == "stuck"
    assert outcome.deliverable == target
    assert target.read_text(encoding="utf-8") == "earned", "the earned file is left alone"
    assert outcome.solved is False, "a detector never proves a task, only a verifier does"


def test_a_repeat_block_with_nothing_on_disk_is_advice_rather_than_an_ending(
    tmp_path: Path,
) -> None:
    """The twin of the stuck guard, missing from the other exit since E031.

    E031 stopped the stuck detector from ending an empty-handed run, but the
    repeat-failure block kept its unconditional `break`. On the GPU stand that exit is
    the one that fires: seven of eighteen fixtures in E052 ended at steps 6-10 of 30,
    35-70 s into a 600 s budget, with `deliverable=none`. A run in that state has
    nothing to protect, so ending it converts a maybe into a certain zero.
    """
    missing = tmp_path / "nowhere" / "file.txt"
    repeated = action("read_range", path=str(missing))
    llm = StubLlm(replies=[repeated for _ in range(limits.MAX_STEPS + 3)])

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert outcome.stop_reason != "stuck"
    assert outcome.stop_reason in {"steps", "budget"}
    assert any("already failed" in text for text in outcome.observations)


def test_the_deliverable_checkpoint_fires_on_steps_when_the_endpoint_is_fast(
    tmp_path: Path,
) -> None:
    """`EARLY_DELIVERABLE_DEADLINE_S` is 90 s and never arrives on a fast endpoint.

    It was written when a call cost 10-40 s, so eight steps carried the run past it. At
    4-8 s a call, thirty steps fit in forty seconds and the checkpoint never fires: the
    seven empty-handed zeros of E052 all died before it. The checkpoint has to be
    reachable by steps as well as by seconds.
    """
    llm = StubLlm(replies=[action("list_dir", path=str(tmp_path)) for _ in range(limits.MAX_STEPS + 3)])

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert any(text.startswith("[бюджет]") for text in outcome.observations)


def test_finish_nested_under_arguments_is_folded_like_every_other_tool(tmp_path: Path) -> None:
    # `write_file(arguments={...})` is folded up, `finish(arguments={...})` was not:
    # the finish branch reads `act["path"]` before `_arguments` normalises anything,
    # so the same wrapper that every other tool survives was refused for "no path".
    target = tmp_path / "answer.txt"
    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content="OK"),
            LlmReply(
                text=json.dumps(
                    {
                        "tool": "finish",
                        "arguments": {
                            "path": str(target),
                            "verify": "",
                            "summary": "done",
                        },
                    }
                )
            ),
        ]
    )

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert outcome.stop_reason == "verified"
    assert outcome.deliverable == target
    assert not any("finish refused: no `path`" in text for text in outcome.observations)


def test_the_audit_checkpoint_asks_for_what_the_observations_support(tmp_path: Path) -> None:
    """P2c. The excerpt and a read both put the vulnerable handler in front of the
    model, and at the 90-second checkpoint it wrote `{"findings": []}` four times and
    stopped. "Write your best now" is answered by an empty list, so for an audit the
    checkpoint has to say what "best" means -- without demanding a finding, because a
    safe application is a legitimate empty report."""
    now = [0.0]
    state = make_state(tmp_path, clock=lambda: now[0])
    llm = StubLlm(replies=[action("list_dir", path=str(tmp_path)) for _ in range(3)])
    now[0] = limits.EARLY_DELIVERABLE_DEADLINE_S + 1

    outcome = loop.run(
        state, llm, FakeVerify(),
        brief=make_brief(tmp_path), playbook=playbooks.CODE_AUDIT,
    )

    # The first checkpoint fires before anything has been observed and says so (see
    # `test_the_checkpoint_asks_for_a_look_before_anything_has_been_observed`); the audit
    # wording belongs to the checkpoints that follow a successful observation.
    checkpoint = [text for text in outcome.observations if "[бюджет]" in text][-1]
    assert "нет проверяемого результата" in checkpoint
    assert "наблюдени" in checkpoint
    assert "пустой" in checkpoint.lower()
    # P2d: the same model in the action envelope produced a real finding whose
    # evidence was a paraphrase, and the organizer check wants the concrete detail.
    assert "evidence" in checkpoint
    assert "строку" in checkpoint


def test_the_checkpoint_of_another_task_is_left_alone(tmp_path: Path) -> None:
    now = [0.0]
    state = make_state(tmp_path, clock=lambda: now[0])
    llm = StubLlm(replies=[action("list_dir", path=str(tmp_path)) for _ in range(3)])
    now[0] = limits.EARLY_DELIVERABLE_DEADLINE_S + 1

    outcome = run(state, llm, FakeVerify())

    # The last one, i.e. the branch that has an observation behind it: that is where the
    # audit wording would appear if the playbook were being ignored.
    checkpoint = [text for text in outcome.observations if "[бюджет]" in text][-1]
    assert "write_file" in checkpoint
    assert "пустой" not in checkpoint.lower()


def test_a_scoped_edit_keeps_the_part_of_the_file_the_model_never_read(tmp_path: Path) -> None:
    # E-BASE: the fix itself was right and the module still stopped importing, because
    # the only way to change a file was to replace all of it with the window that had
    # been read. The edit has to reach the deliverable without touching the rest.
    target = tmp_path / "auth.py"
    target.write_text("import os\n\nrouter = make()\nquery = f\"WHERE u = '{name}'\"\n", encoding="utf-8")
    llm = StubLlm(
        replies=[
            action(
                "replace_text",
                path=str(target),
                old="query = f\"WHERE u = '{name}'\"",
                new='query = "WHERE u = $1"',
            ),
            action("finish", path=str(target), verify="python3 -c ''", summary="готово"),
        ]
    )
    verify = FakeVerify((True, "command", ""))

    outcome = run(make_state(tmp_path), llm, verify)

    after = target.read_text(encoding="utf-8")
    assert after == 'import os\n\nrouter = make()\nquery = "WHERE u = $1"\n'
    assert outcome.deliverable == target
    assert outcome.solved is True


def test_a_successful_edit_shows_the_model_what_it_left_behind(tmp_path: Path) -> None:
    """The anchored edit was the one action whose result nobody could see.

    Five of five failed fix-sqli edits anchored the statement that builds the query and
    not the call that uses it, so a stale `fetchrow(query)` stayed one line below the
    repaired string and the model never learned it was there -- the observation said
    `replaced 1 occurrence at line N` and stopped. Four of four successful edits included
    that call in the anchor. The neighbourhood is the whole difference, and it fits in a
    few lines.
    """
    target = tmp_path / "auth.py"
    target.write_text(
        "import os\n"
        "\n"
        "async def login(name):\n"
        "    query = f\"SELECT * FROM users WHERE u = '{name}'\"\n"
        "    row = await conn.fetchrow(query)\n"
        "    return row\n",
        encoding="utf-8",
    )
    llm = StubLlm(
        replies=[
            action(
                "replace_text",
                path=str(target),
                old="    query = f\"SELECT * FROM users WHERE u = '{name}'\"",
                new='    query = "SELECT * FROM users WHERE u = $1"',
            )
        ]
    )

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    echoed = next(text for text in outcome.observations if "replace_text -> ok" in text)
    assert "после правки" in echoed
    assert 'query = "SELECT * FROM users WHERE u = $1"' in echoed, "the new text is shown"
    assert "await conn.fetchrow(query)" in echoed, "and so is the call the anchor omitted"
    assert "async def login(name):" in echoed, "context above the edit as well"


def test_the_edit_echo_stays_inside_its_own_budget(tmp_path: Path) -> None:
    # Paid on every edit and carried in the observation history, so it is a window onto
    # the edit and never a second read of the file.
    target = tmp_path / "wide.py"
    long_line = "x = '" + "y" * 400 + "'"
    target.write_text("\n".join([long_line] * 12) + "\nanchor = 1\n", encoding="utf-8")
    llm = StubLlm(replies=[action("replace_text", path=str(target), old="anchor = 1", new="anchor = 2")])

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    echoed = next(text for text in outcome.observations if "replace_text -> ok" in text)
    _, _, window = echoed.partition("после правки")
    assert len(window) <= limits.EDIT_ECHO_MAX_CHARS + 120, "bounded whatever the file looks like"
    assert "echo cut" in window


def test_a_refused_edit_echoes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "auth.py"
    target.write_text("import os\n", encoding="utf-8")
    llm = StubLlm(replies=[action("replace_text", path=str(target), old="absent", new="x")])

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert not any("после правки" in text for text in outcome.observations)


def test_the_edit_echo_does_not_change_what_the_detectors_fingerprint(tmp_path: Path) -> None:
    # The echo is appended to the text the model reads, not to the tool output the run
    # hashes: a wider fingerprint would quietly change which repeats the detectors catch.
    target = tmp_path / "auth.py"
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")
    llm = StubLlm(replies=[action("replace_text", path=str(target), old="a = 1", new="a = 3")])
    state = make_state(tmp_path)

    run(state, llm, FakeVerify())

    record = next(item for item in state.actions if item.tool == "replace_text")
    assert record.result_fingerprint == loop.detectors.digest(
        f"replaced 1 occurrence at line 1 of {target}"
    )


# --- the checkpoint has to know whether the run has observed anything ---------


def test_the_checkpoint_asks_for_a_look_before_anything_has_been_observed(
    tmp_path: Path,
) -> None:
    """Case (d). The hint fired on a run that had not read one file, 6/6 times.

    "Write your best attempt now" with no observations behind it is an instruction to
    invent, and the model took it: the forensic answers were fabricated values, different
    every run, so no detector could catch them. Before anything has been observed the
    cheapest correct action is to look, and saying so costs the same step.
    """
    now = [0.0]
    state = make_state(tmp_path, clock=lambda: now[0])
    llm = StubLlm(replies=[action("list_dir", path=str(tmp_path)) for _ in range(3)])
    now[0] = limits.EARLY_DELIVERABLE_DEADLINE_S + 1

    outcome = run(state, llm, FakeVerify())

    first = next(text for text in outcome.observations if "[бюджет]" in text)
    assert "ни одного успешного наблюдения" in first
    assert "не выдумывай" in first.lower()
    assert "целиком задан в условии" in first, "an instruction-determined answer may still be written"
    assert "list_dir, read_range, search" in first


def test_the_checkpoint_asks_for_the_best_attempt_once_something_was_observed(
    tmp_path: Path,
) -> None:
    (tmp_path / "evidence.log").write_text("first line\n", encoding="utf-8")
    now = [0.0]
    state = make_state(tmp_path, clock=lambda: now[0])
    llm = StubLlm(replies=[action("list_dir", path=str(tmp_path)) for _ in range(3)])
    now[0] = limits.EARLY_DELIVERABLE_DEADLINE_S + 1

    outcome = run(state, llm, FakeVerify())

    checkpoints = [text for text in outcome.observations if "[бюджет]" in text]
    assert len(checkpoints) > 1, "the checkpoint fires on every empty-handed step"
    assert "write_file" in checkpoints[-1], "an observed run is asked to produce"
    assert "ни одного успешного наблюдения" not in checkpoints[-1]


def test_a_failed_action_is_not_an_observation_the_checkpoint_can_build_on(
    tmp_path: Path,
) -> None:
    now = [0.0]
    state = make_state(tmp_path, clock=lambda: now[0])
    missing = tmp_path / "nowhere" / "file.txt"
    llm = StubLlm(replies=[action("read_range", path=str(missing)) for _ in range(3)])
    now[0] = limits.EARLY_DELIVERABLE_DEADLINE_S + 1

    outcome = run(state, llm, FakeVerify())

    assert all(
        "ни одного успешного наблюдения" in text
        for text in outcome.observations
        if "[бюджет]" in text
    )


# --- the operator switch that makes the chain visible -------------------------


def test_the_budget_trace_prints_the_thinking_chain_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(limits.TRACE_BUDGET_ENV_NAME, "1")
    state = make_state(tmp_path)
    llm = StubLlm(replies=[
        LlmError("transport timeout: TimeoutError", retryable=True, timed_out=True),
        action("list_dir", path=str(tmp_path)),
    ])

    run(state, llm, FakeVerify())

    printed = capsys.readouterr().out
    assert "agent budget:" in printed
    assert "allow-long-llm" in printed


def test_the_budget_trace_is_off_unless_the_variable_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(limits.TRACE_BUDGET_ENV_NAME, raising=False)
    state = make_state(tmp_path)
    llm = StubLlm(replies=[
        LlmError("transport timeout: TimeoutError", retryable=True, timed_out=True),
        action("list_dir", path=str(tmp_path)),
    ])

    run(state, llm, FakeVerify())

    assert "agent budget:" not in capsys.readouterr().out


def test_the_budget_trace_never_prints_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(limits.TRACE_BUDGET_ENV_NAME, "1")
    state = make_state(tmp_path)
    state.secrets = ("sk-do-not-print",)

    state.record("thinking", "llm", "endpoint said sk-do-not-print while reasoning")

    printed = capsys.readouterr().out
    assert "agent thinking:" in printed
    assert "sk-do-not-print" not in printed


def test_an_edit_whose_anchor_is_not_in_the_file_changes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "auth.py"
    target.write_text("import os\n", encoding="utf-8")
    llm = StubLlm(replies=[action("replace_text", path=str(target), old="absent", new="x")])

    outcome = run(make_state(tmp_path), llm, verify=FakeVerify())

    assert target.read_text(encoding="utf-8") == "import os\n"
    assert outcome.solved is False


def test_an_exact_file_finish_uses_the_instruction_oracle_before_a_reader_command(
    tmp_path: Path,
) -> None:
    target = tmp_path / "answer.txt"
    state = AgentState(
        instruction=(
            f"Create a file at `{target}` whose entire content is exactly the single "
            "word `Expected` (the file must contain only those eight characters)."
        ),
        workspace=tmp_path,
        budget=Budget(total_s=600.0, clock=lambda: 0.0),
        named_paths=(target,),
    )
    from agent.verify import verify as real_verify

    llm = StubLlm(
        replies=[
            action("write_file", path=str(target), content="Expected"),
            action("finish", path=str(target), verify=f"cat {target}", summary="done"),
        ]
    )

    outcome = loop.run(
        state,
        llm,
        real_verify,
        brief=make_brief(tmp_path),
        playbook=playbooks.GENERAL,
    )

    assert outcome.solved is True
    assert outcome.verdict is not None
    assert outcome.verdict.stage == "exact-file"


def test_a_successful_exact_file_write_finishes_without_an_extra_model_turn(
    tmp_path: Path,
) -> None:
    target = tmp_path / "answer.txt"
    state = AgentState(
        instruction=(
            f"Create a file at `{target}` whose entire content is exactly the single "
            "word `Expected` (the file must contain only those eight characters)."
        ),
        workspace=tmp_path,
        budget=Budget(total_s=600.0, clock=lambda: 0.0),
        named_paths=(target,),
    )
    from agent.verify import verify as real_verify

    llm = StubLlm(replies=[action("write_file", path=str(target), content="Expected")])

    outcome = loop.run(
        state,
        llm,
        real_verify,
        brief=make_brief(tmp_path),
        playbook=playbooks.GENERAL,
    )

    assert len(llm.calls) == 1, "a green exact-file write must not spend another model turn"
    assert outcome.solved is True
    assert outcome.stop_reason == "verified"
    assert outcome.deliverable == target
    assert outcome.verdict is not None and outcome.verdict.stage == "exact-file"


@pytest.mark.parametrize("playbook", playbooks.PLAYBOOKS, ids=lambda item: item.name)
def test_exact_file_write_fast_path_is_reserved_for_general_tasks(
    tmp_path: Path, playbook: playbooks.Playbook
) -> None:
    target = tmp_path / "answer.txt"
    state = AgentState(
        instruction=(
            f"Create a file at `{target}` whose entire content is exactly the single "
            "word `Expected` (the file must contain only those eight characters)."
        ),
        workspace=tmp_path,
        budget=Budget(total_s=600.0, clock=lambda: 0.0),
        named_paths=(target,),
    )
    state.budget.max_steps = 1
    llm = StubLlm(replies=[action("write_file", path=str(target), content="Expected")])
    verify = FakeVerify((True, "exact-file", "passed"))

    outcome = loop.run(
        state,
        llm,
        verify,
        brief=make_brief(tmp_path),
        playbook=playbook,
    )

    if playbook.name == "general":
        assert outcome.solved is True
        assert outcome.stop_reason == "verified"
        assert verify.calls == [(target, "")]
    else:
        assert outcome.solved is False
        assert outcome.stop_reason != "verified"


def test_exact_file_fast_path_records_one_empty_command_verification(
    tmp_path: Path,
) -> None:
    target = tmp_path / "answer.txt"
    state = AgentState(
        instruction=(
            f"Create a file at `{target}` whose entire content is exactly the single "
            "word `Expected` (the file must contain only those eight characters)."
        ),
        workspace=tmp_path,
        budget=Budget(total_s=600.0, clock=lambda: 0.0),
        named_paths=(target,),
    )
    llm = StubLlm(replies=[action("write_file", path=str(target), content="Expected")])
    verify = FakeVerify((True, "exact-file", "passed"))

    outcome = loop.run(
        state,
        llm,
        verify,
        brief=make_brief(tmp_path),
        playbook=playbooks.GENERAL,
    )

    assert outcome.solved is True
    assert verify.calls == [(target, "")]
    phase_names = [(event.kind, event.name, event.detail) for event in state.audit]
    assert phase_names.count(("phase", "verify", "start")) == 1
    assert phase_names.count(("phase", "verify", "end")) == 1
    assert sum(event.kind == "verify" for event in state.audit) == 1


def test_a_server_body_mentioning_a_timeout_is_a_hard_failure() -> None:
    # The classification turns on the typed flag, never on the reason text: a 504 body
    # or a 400 about a timeout parameter is an endpoint answering instantly (critic 16.09).
    from agent.loop import HARD_FAILURE, SLOW_CALL, _llm_failure_kind
    from agent.ports import LlmError
    assert _llm_failure_kind(LlmError("endpoint returned 504: upstream request timeout", retryable=True)) == HARD_FAILURE
    assert _llm_failure_kind(LlmError("endpoint returned 400: model timeout parameter invalid", retryable=False)) == HARD_FAILURE
    assert _llm_failure_kind(LlmError("transport timeout: TimeoutError", retryable=True, timed_out=True)) == SLOW_CALL
    assert _llm_failure_kind(LlmError("transport timeout: TimeoutError", retryable=True, timed_out=True, output_tokens=5)) == HARD_FAILURE


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c \"import profile_cache; print(profile_cache.render_profile('a', 'u', 1))\"",
        "python3 -c 'import importlib.util, sys\nif \"m\" in sys.modules: del sys.modules[\"m\"]\nimport m\nassert m.f(1) == 2, \"leak\"'",
        "python3 {ws}/verify_fix.py",
        "python3 -B verify_fix.py",
        "cd {ws} && python3 verify_fix.py",
        "python3 -c 'import sys\nsys.path.insert(0, \"{ws}\")\nimport m\nprint(m)'",
        "cd {ws} && python3 -c 'import m\n# Test 1: cache\nassert m.f(1) == m.f(1), f\"miss {{m}}\"\nprint(\"ok\")'",
        "python3 -c 'import os; print(os.getcwd(), os.path.exists(\"m.py\"), os.listdir(\".\"))'",
        "python3 -c 'print(\"a,b\".replace(\",\", \";\"))'",
        "python3 -c 'import m; print(getattr(m, \"f\"))'",
        "python3 -c 'import sqlite3; print(sqlite3.connect(\":memory:\").execute(\"select 1\").fetchone())'",
        # shell-looking characters inside the quoted payload are Python, not a pipeline
        "python3 -c 'import m\nassert m.f(1) == 2; print(\"a | b > c\")\n# x -> y; z && w'",
        "cd {ws} && python3 -c \"from m import f\n# Test cases\ntests = [('https://h.example/test?x=1', True)]\nfor u, ok in tests:\n    assert (f(u) == ok)\n\"",
    ],
)
def test_code_fix_lets_a_read_only_python_probe_through(tmp_path: Path, command: str) -> None:
    # On a fix task without a test suite the model's only check is to import the patched
    # module and call it. Refusing every `python -c` and every local script left it with
    # nothing (EXTRA-taxonomy: 6-12 refusals per fix episode). What the probe does to the
    # evaluator is judged by effect (below), so ordinary reads are not second-guessed.
    (tmp_path / "verify_fix.py").write_text(
        "import importlib.util\nspec = importlib.util.spec_from_file_location('m', 'm.py')\n"
        "print(spec)\nassert spec is not None\n",
        encoding="utf-8",
    )
    state = make_state(tmp_path)
    assert loop._code_fix_shell_refusal(command.format(ws=tmp_path), state) == ""


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c \"open('tests/test_api.py', 'w').write('')\"",
        "python3 -c 'print(open(\"tests/test_api.py\").read())'",
        "python3 -c 'import shutil; shutil.rmtree(\"x\")'",
        "python3 -c 'import subprocess; subprocess.run([\"rm\", \"-r\", \"x\"])'",
        "python3 -c 'import os; os.system(\"rm -r x\")'",
        "python3 -c 'exec(\"import os\")'",
        "python3 -c '__import__(\"o\" + \"s\").remove(\"x\")'",
        "python3 {ws}/tests/conftest.py",
        "python3 {ws}/mutate.py",
        "python3 -c 'print(1)' > tests/out.txt",
        "python3 -c 'print(1)' | tee tests/out.txt",
        "python3 -c 'print(1)'; rm -r tests",
        "python3 -c 'print(1)' $(rm -r tests)",
        "python3 -W ignore -c 'print(1)'",
        "cd /etc && python3 -c 'print(1)'",
        "cd {ws} && python3 -c 'print(1)' && rm -r tests",
        "python3 -c 'print(open(\".git/HEAD\").read())'",
    ],
)
def test_code_fix_still_refuses_a_python_snippet_that_spawns_or_names_the_evaluator(
    tmp_path: Path, command: str
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "mutate.py").write_text("import shutil\nshutil.rmtree('tests')\n", encoding="utf-8")
    state = make_state(tmp_path)
    assert loop._code_fix_shell_refusal(command.format(ws=tmp_path), state) != ""


def test_a_python_script_outside_the_workspace_is_not_a_probe(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("print(1)\n", encoding="utf-8")
    try:
        assert loop._code_fix_shell_refusal(f"python3 {outside}", make_state(tmp_path)) != ""
    finally:
        outside.unlink()


def _probe(command: str, state: AgentState):
    from agent import playbooks

    return loop._execute("run_shell", {"command": command}, state, playbook=playbooks.CODE_FIX)


@pytest.mark.parametrize(
    "command",
    [
        # a variable file mode and a chr()-built name pass any text filter
        "python3 -c \"n=chr(99)+'onftest.py'; d=chr(116)+'ests'; m=chr(119); open(d+chr(47)+n, m).close()\"",
        # a side-effecting import: the snippet's own text is clean
        "python3 -c 'import helper'",
    ],
)
def test_a_probe_that_changes_the_evaluator_is_undone_and_ends_the_privilege(
    tmp_path: Path, command: str
) -> None:
    (tmp_path / "tests").mkdir()
    original = "def pytest_configure(config):\n    pass\n"
    (tmp_path / "tests" / "conftest.py").write_text(original, encoding="utf-8")
    (tmp_path / "helper.py").write_text(
        "p = 'tests/' + 'conf' + 'test.py'\nopen(p, 'w').write('')\n", encoding="utf-8"
    )
    state = make_state(tmp_path)

    result = _probe(command, state)

    assert result.ok is False
    assert "restored" in result.detail and "conftest.py" in result.detail
    assert (tmp_path / "tests" / "conftest.py").read_text(encoding="utf-8") == original
    assert state.python_probes_disabled is True
    assert loop._code_fix_shell_refusal("python3 -c 'print(1)'", state) != ""


@pytest.mark.parametrize(
    "command, added",
    [
        ("python3 -c \"open('te' + 'sts/te' + 'st_b.py', 'w').write('def test_b(): pass')\"", "test_b.py"),
        # sqlite writes a file wherever it is told to
        ("python3 -c \"import sqlite3; sqlite3.connect('te' + 'sts/new.db').execute('create table t(x)')\"", "new.db"),
    ],
)
def test_a_probe_that_adds_an_evaluator_file_has_it_removed(tmp_path: Path, command: str, added: str) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
    state = make_state(tmp_path)

    result = _probe(command, state)

    assert result.ok is False and added in result.detail
    assert not (tmp_path / "tests" / added).exists()
    assert (tmp_path / "tests" / "test_a.py").exists()
    assert state.python_probes_disabled is True


def test_a_probe_that_touches_git_metadata_is_reported_and_ends_the_privilege(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    state = make_state(tmp_path)

    result = _probe("python3 -c \"d='.g'+'it'; open(d+'/HEAD', 'w').write('ref: refs/heads/x')\"", state)

    assert result.ok is False and "HEAD" in result.detail
    assert (tmp_path / ".git" / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main\n"
    assert state.python_probes_disabled is True


def test_a_symlink_smuggled_into_the_evaluator_is_removed(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("def test_z(): assert False\n", encoding="utf-8")
    state = make_state(tmp_path)

    result = _probe("python3 -c \"import os; os.symlink('../app.py', 'te' + 'sts/te' + 'st_link.py')\"", state)

    assert result.ok is False and "test_link.py" in result.detail
    assert not (tmp_path / "tests" / "test_link.py").is_symlink()
    assert (tmp_path / "app.py").exists()


def test_a_symlink_already_in_the_evaluator_is_kept_and_put_back_if_replaced(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "shared_test.py").write_text("def test_s(): pass\n", encoding="utf-8")
    link = tmp_path / "tests" / "test_shared.py"
    link.symlink_to("../shared_test.py")
    state = make_state(tmp_path)

    harmless = _probe("python3 -c 'print(1)'", state)
    assert harmless.ok is True and state.python_probes_disabled is False

    replaced = _probe(
        "python3 -c \"import os; p='te'+'sts/te'+'st_shared.py'; os.unlink(p); open(p, 'w').write('x')\"", state
    )
    assert replaced.ok is False and "test_shared.py" in replaced.detail
    assert link.is_symlink() and os.readlink(link) == "../shared_test.py"


def test_a_probe_that_changes_a_mode_gets_the_mode_restored(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    target = tmp_path / "tests" / "test_a.py"
    target.write_text("def test_a(): pass\n", encoding="utf-8")
    target.chmod(0o644)
    state = make_state(tmp_path)

    result = _probe("python3 -c \"import os; os.chmod('te' + 'sts/te' + 'st_a.py', 0)\"", state)

    assert result.ok is False and "test_a.py" in result.detail and "could not" not in result.detail
    assert (target.stat().st_mode & 0o777) == 0o644
    assert target.read_text(encoding="utf-8") == "def test_a(): pass\n"


def test_a_watched_root_is_reported_but_not_restored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = tmp_path.parent / f"{tmp_path.name}-logs"
    logs.mkdir()
    (logs / "reward.txt").write_text("0\n", encoding="utf-8")
    monkeypatch.setattr(loop, "_EXTRA_PROTECTED_ROOTS", (logs,))
    state = make_state(tmp_path)

    result = _probe(f"python3 -c \"open('{logs}/reward.txt', 'w').write('1')\"", state)

    assert result.ok is False and "watched, not restored" in result.detail
    assert (logs / "reward.txt").read_text(encoding="utf-8") == "1"
    assert state.python_probes_disabled is True


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'import os; os.posix_spawn(\"/bin/rm\", [\"rm\", \"x\"], {})'",
        "python3 -c 'import multiprocessing as mp; mp.Process(target=print, daemon=True).start()'",
    ],
)
def test_spawning_through_posix_spawn_or_multiprocessing_is_refused_up_front(tmp_path: Path, command: str) -> None:
    assert loop._code_fix_shell_refusal(command, make_state(tmp_path)) != ""


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'print(1)' && python3 -m pytest -q",
        "cd {ws} && python3 -c 'import m' && python3 -m pytest tests/ -v 2>&1 | head -80",
        "python3 -m pytest -q ; python3 -c 'print(1)'",
        "python3 -c 'print(1)' && git status",
    ],
)
def test_a_probe_mixed_with_other_commands_is_refused_with_the_way_out(tmp_path: Path, command: str) -> None:
    # The snapshot covers the whole line: a fixture database pytest writes or the
    # stat cache git refreshes would be charged to the probe (found by the critic).
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    state = make_state(tmp_path)

    result = _probe(command.format(ws=tmp_path), state)

    assert result.ok is False and "as its own command" in result.detail
    assert state.python_probes_disabled is False


@pytest.mark.parametrize("command", ["cd ~ && python3 -c 'print(1)'", "cd - && python3 -c 'print(1)'", "cd $HOME && python3 -c 'print(1)'", "cd {ws}/missing && python3 -c 'print(1)'"])
def test_a_cd_that_is_not_a_workspace_directory_is_not_stripped(tmp_path: Path, command: str) -> None:
    assert loop._code_fix_shell_refusal(command.format(ws=tmp_path), make_state(tmp_path)) != ""


def test_a_harmless_probe_runs_and_keeps_the_privilege(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "m.py").write_text("def f(x):\n    return x + 1\n", encoding="utf-8")
    state = make_state(tmp_path)

    result = _probe("python3 -c 'import m; assert m.f(1) == 2; print(\"ok\")'", state)

    assert result.ok is True and "ok" in result.output
    assert state.python_probes_disabled is False


def test_code_audit_reads_carry_line_numbers_and_fix_reads_do_not(tmp_path: Path) -> None:
    # An audit report cites file:line; without numbers in the read the model counted
    # lines itself and was off by one on every extra audit task (EXTRA-taxonomy).
    source = tmp_path / "client.py"
    source.write_text("a = 1\n\nTOKEN = 'x'\n", encoding="utf-8")
    state = make_state(tmp_path)

    audit = loop._execute(
        "read_range", {"path": str(source), "start_line": 1}, state, playbook=playbooks.CODE_AUDIT
    )
    fix = loop._execute(
        "read_range", {"path": str(source), "start_line": 1}, state, playbook=playbooks.CODE_FIX
    )

    assert audit.ok and fix.ok
    assert audit.output.startswith("1: a = 1\n2: \n3: TOKEN = 'x'\n\n[read_range source=")
    assert "end_line=3" in audit.output
    assert fix.output.startswith("a = 1\n\nTOKEN = 'x'\n\n[read_range source=")


def test_numbered_reads_start_at_the_window_and_keep_the_cut_marker() -> None:
    output = "def f():\n    pass\n… [window cut at line 41] …\n[read_range source=/x start_line=40 end_line=41 eof=no truncated=yes]"
    numbered = loop._number_read_lines(output, 40)
    assert numbered == (
        "40: def f():\n41:     pass\n… [window cut at line 41] …\n"
        "[read_range source=/x start_line=40 end_line=41 eof=no truncated=yes]"
    )


# --- composed read-only commands ---------------------------------------------------


def _fix_workspace(root: Path) -> AgentState:
    (root / "app").mkdir()
    (root / "tests").mkdir()
    (root / "main.py").write_text("from app.db import q\n", encoding="utf-8")
    (root / "app" / "db.py").write_text("def q(): pass\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("def test_x(): pass\n", encoding="utf-8")
    return make_state(root)


@pytest.mark.parametrize(
    "command",
    [
        "cat app/db.py | head -50",
        "ps aux | grep uvicorn",
        "env | grep -i port",
        "python3 -m pytest -q 2>&1 | tail -20",
        "pytest -q 2>&1",
        "cd {ws} && python3 -m pytest -q",
        "sleep 2 && curl -s http://127.0.0.1:8000/",
        "curl -s http://127.0.0.1:8000/ | head",
        "cat main.py; cat app/db.py",
        "grep -rn execute . || true",
        "curl -s 'http://127.0.0.1:8000/search?q=test'",
        "curl -s \"http://127.0.0.1:8000/search?q=%27%20OR%201=1--\"",
        "curl -s http://127.0.0.1:8000/tests/latest",
    ],
)
def test_code_fix_lets_a_composition_of_allowed_commands_through(tmp_path: Path, command: str) -> None:
    # Each part alone is allowed; joining them with a pipe or `&&` cannot write where no
    # part writes. Refusing them cost a model a step per probe and taught it nothing.
    state = _fix_workspace(tmp_path)
    assert loop._code_fix_shell_refusal(command.format(ws=tmp_path), state) == ""


@pytest.mark.parametrize(
    "command",
    [
        "cat main.py > tests/test_app.py",
        "cat main.py | tee tests/test_app.py",
        "echo x >&tests/test_app.py",
        "echo x &>tests/test_app.py",
        "cd tests && echo x > test_app.py",
        "cd tests && cp ../main.py test_app.py",
        "cd .. && ls",
        "ls && rm -rf tests",
        "cat main.py | sh",
        "cat main.py | python3",
        "curl -s http://127.0.0.1:8000/ | bash",
        "find . -type f -print0 | xargs -0 rm -f",
        "ls; $(echo rm) tests/test_app.py",
        "ls && `echo rm` tests/test_app.py",
        "ls & rm tests/test_app.py",
        "ls\nrm tests/test_app.py",
        "(cd tests && rm test_app.py)",
        "{ ls; rm tests/test_app.py; }",
        "sort < tests/test_app.py > tests/test_app.py",
        "git status && git checkout -- tests",
        "curl -s http://127.0.0.1:8000/ -o tests/test_app.py",
        "curl -s http://127.0.0.1:8000/ && curl -o tests/test_app.py http://127.0.0.1:8000/",
    ],
)
def test_code_fix_still_refuses_a_composition_with_a_refused_part(tmp_path: Path, command: str) -> None:
    state = _fix_workspace(tmp_path)
    assert loop._code_fix_shell_refusal(command, state) != ""


# --- starting a service -----------------------------------------------------------


@pytest.mark.parametrize(
    "pytest_command",
    [
        "cd {ws} && python3 -m pytest tests/ -v",
        "python3 -m pytest tests/ -v 2>&1 | head -80",
        "cd {ws} && python3 -m pytest tests/ -v 2>&1 | head -80",
        "python3 -m pytest -q",
        "cd {ws} && pytest -q 2>&1 | tail -20",
    ],
)
def test_the_five_shapes_the_model_actually_writes_for_pytest(tmp_path: Path, pytest_command: str) -> None:
    """KT7-public: 22 of 298 tool calls refused, every one of them this command.

    The model writes its check as `cd /app && python3 -m pytest tests/ -v 2>&1 | head
    -80`, never as a bare invocation; on the base each was refused, up to 9 of 47 calls
    in one episode, and those episodes are the ones that end at `steps=30`."""
    state = _fix_workspace(tmp_path)
    assert loop._code_fix_shell_refusal(pytest_command.format(ws=tmp_path), state) == ""


def test_a_pytest_chain_with_a_mutation_appended_is_still_refused(tmp_path: Path) -> None:
    state = _fix_workspace(tmp_path)
    assert loop._code_fix_shell_refusal(f"cd {tmp_path} && python3 -m pytest tests/ ; rm -r tests", state) != ""


def test_a_probe_inside_a_composition_is_still_guarded(tmp_path: Path) -> None:
    """The refusal judges a composition part by part, so the snapshot must too.

    Otherwise the pipe form runs the model's Python with no snapshot behind it:
    `_python_probe` is False for the whole string (it holds an operator) while the
    refusal has already let it through on the strength of its parts."""
    state = _fix_workspace(tmp_path)
    command = 'python3 -c "import app.db" | head -5'
    assert loop._code_fix_shell_refusal(command, state) == ""
    assert not loop._python_probe(command, state), "the whole string is not a lone probe"
    parts = loop._code_fix_shell_parts(command, state) or [command]
    assert any(loop._python_probe(part, state) for part in parts), "the guard would miss it"
