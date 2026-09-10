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
    assert any("по кругу" in text for text in outcome.observations)


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

    checkpoint = next(text for text in outcome.observations if "[бюджет]" in text)
    assert "нет проверяемого результата" in checkpoint
    assert "наблюдени" in checkpoint
    assert "пустой" in checkpoint.lower()


def test_the_checkpoint_of_another_task_is_left_alone(tmp_path: Path) -> None:
    now = [0.0]
    state = make_state(tmp_path, clock=lambda: now[0])
    llm = StubLlm(replies=[action("list_dir", path=str(tmp_path)) for _ in range(3)])
    now[0] = limits.EARLY_DELIVERABLE_DEADLINE_S + 1

    outcome = run(state, llm, FakeVerify())

    checkpoint = next(text for text in outcome.observations if "[бюджет]" in text)
    assert "пустой" not in checkpoint.lower()
