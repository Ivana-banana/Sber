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
from dataclasses import dataclass
from pathlib import Path

from agent import limits, loop, playbooks
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
) -> loop.Outcome:
    return loop.run(
        state,
        llm,
        verify or FakeVerify(),
        brief=make_brief(state.workspace),
        playbook=playbooks.FORENSICS,
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


def test_a_loop_that_goes_in_circles_stops_itself(tmp_path: Path) -> None:
    llm = StubLlm(replies=[action("list_dir", path=str(tmp_path)) for _ in range(20)])

    outcome = run(make_state(tmp_path), llm, FakeVerify())

    assert outcome.stop_reason == "stuck"
    assert outcome.solved is False
    assert len(llm.calls) < limits.MAX_STEPS, "stopping early is the whole point"


def test_the_step_cap_ends_a_model_that_never_finishes(tmp_path: Path) -> None:
    llm = StubLlm(
        replies=[
            action("write_file", path=str(tmp_path / f"f{index}.txt"), content=f"{index}")
            for index in range(limits.MAX_STEPS + 5)
        ]
    )
    state = make_state(tmp_path)

    outcome = run(state, llm, FakeVerify())

    assert state.budget.steps_used == limits.MAX_STEPS
    assert outcome.stop_reason == "steps"


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
            action("finish", path=str(tmp_path), verify="", summary="ok"),
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


def test_a_stuck_run_holding_a_readable_file_is_still_a_zero(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("something", encoding="utf-8")
    llm = StubLlm(replies=[action("list_dir", path=str(tmp_path)) for _ in range(20)])
    state = make_state(tmp_path)
    state.artifacts.append(target)

    outcome = run(state, llm, FakeVerify((True, "unverified", "")))

    assert outcome.stop_reason == "stuck"
    assert outcome.solved is False


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
