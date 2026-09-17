"""Одна реплика модели — один шаг, но не обязательно одно действие.

Измерено на стенде Qwen3.6-35B-A3B (JOURNAL, 16.09, трейсы под
`build/afk-q36-treatment/logs`): на задачах исправления кода модель отвечает почти
каждый шаг тремя-шестью `tool_calls` сразу, клиент брал первый и молча терял
остальные, и модель запрашивала ту же пачку снова. В одном эпизоде 49 вызовов из 51
оказались чтениями, ни одной правки сделано не было, а закончился он по лимиту шагов:
на быстром endpoint именно шаги, а не время, — связывающее ограничение.

Эти тесты описывают цену, которую мы согласны за это платить: пачка выполняется
внутри одного шага, но только пока каждое действие read-only. Первое действие,
способное что-то изменить, останавливает пачку и остаётся невыполненным — контракт
«одно изменяющее действие на шаг» не двигается ни на миллиметр.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from agent import limits, llm, loop
from agent.ports import LlmReply
from agent.state import AgentState
from tests.stub_llm import StubLlm
from tests.test_loop import FakeVerify, action, make_state, run


def batched(*calls: LlmReply) -> LlmReply:
    """Fold several actions into the one reply that carried them.

    `agent.llm` hands the loop exactly this: the first call as `text`, the rest as the
    raw argument JSON of calls 2..N. Built here rather than in `StubLlm` so the stub
    stays a transcript player and nothing about the batch contract lives in it.
    """
    first, *rest = calls
    return replace(first, extra_actions=tuple(call.text for call in rest))


def read(path: Path) -> LlmReply:
    return action("read_range", path=str(path), start_line=1)


def make_file(directory: Path, name: str, *, size: int = 40) -> Path:
    target = directory / name
    target.write_text(f"{name}\n" + ("x" * size + "\n") * 3, encoding="utf-8")
    return target


def tools_run(state: AgentState) -> list[str]:
    """Every tool that ran, primary and batch alike, from the audit trail.

    `state.actions` is the detector window and deliberately holds only primary actions:
    a batch of reads must not push the primaries out of it (critic, 16.09).
    """
    return [event.name for event in state.audit if event.kind == "tool"]


def batch_entry(outcome: loop.Outcome) -> str:
    """The one history entry a step's batch produces ('' when there was none)."""
    entries = [text for text in outcome.observations if text.startswith("[step ") and "." in text.split("]")[0]]
    assert len(entries) <= 1, entries
    return entries[0] if entries else ""


def step_markers(outcome: loop.Outcome) -> list[str]:
    """The `[step N]` / `[step N.k]` prefix of every tool observation, in order."""
    return [
        text.split("]")[0] + "]" for text in outcome.observations if text.startswith("[step ")
    ]


def batch_notes(state: AgentState) -> list[str]:
    return [
        event.detail
        for event in state.audit
        if event.kind == "budget" and event.name == "batch"
    ]


# --- what the batch buys ------------------------------------------------------


def test_a_batch_of_reads_runs_inside_one_step(tmp_path: Path) -> None:
    # The whole point: five reads for one step of the budget and one LLM call.
    files = [make_file(tmp_path, f"router_{index}.py") for index in range(5)]
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(replies=[batched(*(read(path) for path in files))])

    outcome = run(state, llm, FakeVerify())

    assert state.budget.steps_used == 1, "a reply is one step, however many calls it carried"
    assert len(llm.calls) == 1, "the extras cost no second call and no extra tokens"
    assert tools_run(state) == ["read_range"] * 5
    assert step_markers(outcome) == ["[step 1]", "[step 1.2]"], "one batch, one history entry"
    entry = batch_entry(outcome)
    assert all(f"[step 1.{k}]" in entry for k in (2, 3, 4, 5))
    assert len(state.actions) == 1, "extras stay out of the detector window"
    assert batch_notes(state) == ["executed 4 of 4 extra calls"]


def test_a_batch_longer_than_the_cap_is_cut_at_the_cap(tmp_path: Path) -> None:
    files = [make_file(tmp_path, f"m{index}.py") for index in range(limits.BATCH_READONLY_MAX + 3)]
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(replies=[batched(*(read(path) for path in files))])

    run(state, llm, FakeVerify())

    assert len(tools_run(state)) == 1 + limits.BATCH_READONLY_MAX
    assert batch_notes(state) == [f"executed {limits.BATCH_READONLY_MAX} of 6 extra calls"]


# --- and what it must never buy ----------------------------------------------


def test_a_write_in_the_batch_stops_it_and_is_not_executed(tmp_path: Path) -> None:
    # The line this feature must not cross. A read costs a re-read at worst; a write
    # executed as somebody's second action in a step is a state change nothing gated.
    first = make_file(tmp_path, "a.py")
    second = make_file(tmp_path, "b.py")
    target = tmp_path / "report.json"
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(
        replies=[
            batched(
                read(first),
                read(second),
                action("write_file", path=str(target), content="{}"),
                read(first),
            )
        ]
    )

    run(state, llm, FakeVerify())

    assert target.exists() is False, "the write is left for its own step"
    assert state.artifacts == []
    assert tools_run(state) == ["read_range", "read_range"], "the batch stopped at the write"
    assert batch_notes(state) == ["executed 1 of 3 extra calls"]
    assert any(
        event.name == "batch-stop" and "write_file" in event.detail for event in state.audit
    )


def test_a_shell_command_in_the_batch_stops_it_even_when_it_only_reads(tmp_path: Path) -> None:
    # `run_shell` is excluded by tool name, not by inspecting the command: whether a
    # command reads or writes is a guess, and a guess must not authorise a second
    # action inside one step.
    source = make_file(tmp_path, "a.py")
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(replies=[batched(read(source), action("run_shell", command="cat a.py"))])

    run(state, llm, FakeVerify())

    assert tools_run(state) == ["read_range"]
    assert batch_notes(state) == ["executed 0 of 1 extra calls"]


def test_a_finish_in_the_batch_is_never_executed(tmp_path: Path) -> None:
    source = make_file(tmp_path, "a.py")
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    verify = FakeVerify()
    llm = StubLlm(
        replies=[
            batched(read(source), action("finish", path=str(source), verify="grep -q x a.py"))
        ]
    )

    outcome = run(state, llm, verify)

    assert outcome.solved is False
    assert verify.calls == [], "only the step's own action may claim the task is done"


def test_extras_are_dropped_when_the_primary_action_never_ran(tmp_path: Path) -> None:
    # A refused `finish` ends the step before anything is executed. The batch belongs
    # to an action that happened; without one the model simply asks again.
    source = make_file(tmp_path, "a.py")
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(
        replies=[batched(action("finish", path=str(tmp_path / "missing.txt")), read(source))]
    )

    run(state, llm, FakeVerify())

    assert state.actions == []
    assert batch_notes(state) == []


# --- duplicates, budget, epoch, grounding -------------------------------------


def test_a_duplicate_inside_the_batch_runs_once(tmp_path: Path) -> None:
    first = make_file(tmp_path, "a.py")
    second = make_file(tmp_path, "b.py")
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(
        replies=[batched(read(first), read(first), read(second), read(second))]
    )

    outcome = run(state, llm, FakeVerify())

    assert tools_run(state) == ["read_range", "read_range"], "a repeat in one epoch adds nothing"
    assert batch_notes(state) == ["executed 1 of 3 extra calls"]
    # The marker names the call's place in the reply, not its place in the queue: the
    # skipped copies keep their numbers so a trace can be read against the reply.
    assert step_markers(outcome) == ["[step 1]", "[step 1.3]"]
    assert any(event.name == "batch-repeat" for event in state.audit)


def test_a_repeat_of_an_earlier_step_is_skipped_inside_the_batch(tmp_path: Path) -> None:
    # Nothing mutated between the steps, so the file still says what the history says.
    first = make_file(tmp_path, "a.py")
    second = make_file(tmp_path, "b.py")
    state = make_state(tmp_path)
    state.budget.max_steps = 2
    llm = StubLlm(replies=[read(first), batched(read(second), read(first))])

    run(state, llm, FakeVerify())

    assert batch_notes(state) == ["executed 0 of 1 extra calls"]
    assert len(tools_run(state)) == 2


def test_the_batch_respects_the_observation_budget_on_large_files(tmp_path: Path) -> None:
    files = [make_file(tmp_path, f"big_{index}.py", size=4000) for index in range(5)]
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(replies=[batched(*(read(path) for path in files))])

    outcome = run(state, llm, FakeVerify())

    entry = batch_entry(outcome)
    assert all(f"[step 1.{k}]" in entry for k in range(2, 2 + limits.BATCH_READONLY_MAX))
    share = limits.BATCH_OBSERVATION_MAX_CHARS // limits.BATCH_READONLY_MAX
    assert len(entry) <= limits.BATCH_OBSERVATION_MAX_CHARS
    assert limits.BATCH_OBSERVATION_MAX_CHARS < limits.TOOL_OUTPUT_HEAD_BYTES + limits.TOOL_OUTPUT_TAIL_BYTES
    # The bound to beat: what one wide `read_range` may put in the history today.
    primary = max(len(text) for text in outcome.observations if text.startswith("[step 1]"))
    assert primary > share, "the fixture is big enough for the clipping to matter"
    # Clipping keeps both ends, and the end is where `read_range` puts the path and
    # line range. A window the model cannot locate is not an observation.
    assert entry.count("[read_range source=") == limits.BATCH_READONLY_MAX


def test_a_read_only_batch_does_not_advance_the_state_epoch(tmp_path: Path) -> None:
    files = [make_file(tmp_path, f"a{index}.py") for index in range(4)]
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(replies=[batched(*(read(path) for path in files))])

    run(state, llm, FakeVerify())

    assert state.state_epoch == 0, "nothing in a batch of reads can invalidate an observation"


def test_extras_after_a_write_read_the_new_state_without_a_second_epoch(tmp_path: Path) -> None:
    source = make_file(tmp_path, "a.py")
    target = tmp_path / "patched.py"
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(
        replies=[batched(action("write_file", path=str(target), content="fixed\n"), read(source))]
    )

    run(state, llm, FakeVerify())

    assert target.read_text(encoding="utf-8") == "fixed\n"
    assert state.state_epoch == 1, "the write opened the epoch; the read did not open another"
    assert tools_run(state) == ["write_file", "read_range"]


def test_a_batch_read_counts_as_a_grounded_observation(tmp_path: Path) -> None:
    # The checkpoint hint is the only reader of the grounded counter, and it says two
    # different things: with nothing observed it forbids inventing values, with an
    # observation behind it asks for the best answer so far. A read executed inside a
    # batch has to move the run from the first sentence to the second.
    source = make_file(tmp_path, "a.py")
    missing = tmp_path / "nope.py"
    state = make_state(tmp_path)
    state.budget.max_steps = 2
    state.budget.early_deadline_s = 0.0
    llm = StubLlm(replies=[batched(read(missing), read(source)), read(missing)])

    outcome = run(state, llm, FakeVerify())

    checkpoints = [text for text in outcome.observations if text.startswith("[бюджет]")]
    assert len(checkpoints) == 2
    assert "ни одного успешного наблюдения" in checkpoints[0], "the primary read failed"
    assert "ни одного успешного наблюдения" not in checkpoints[1], "the batch read grounded it"


def test_the_batch_stops_when_the_step_has_no_time_left(tmp_path: Path) -> None:
    files = [make_file(tmp_path, f"a{index}.py") for index in range(4)]
    ticks = iter([0.0] * 12 + [10_000.0] * 64)
    state = make_state(tmp_path, clock=lambda: next(ticks))
    state.budget.max_steps = 1
    llm = StubLlm(replies=[batched(*(read(path) for path in files))])

    run(state, llm, FakeVerify())

    assert any(
        event.name == "batch-stop" and "no time left" in event.detail for event in state.audit
    )


# --- policy that the batch inherits rather than invents -----------------------


def test_an_extra_reading_outside_the_workspace_is_treated_as_a_primary_read_is(
    tmp_path: Path,
) -> None:
    # Documented, not introduced here: a read of an absolute path is not confined to
    # the workspace for the step's own action either, and a batch must not be a second,
    # quieter policy. If that confinement is ever wanted it belongs on `_execute`.
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("neighbour\n", encoding="utf-8")
    source = make_file(tmp_path, "a.py")
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(replies=[batched(read(source), read(outside))])

    outcome = run(state, llm, FakeVerify())

    assert tools_run(state) == ["read_range", "read_range"]
    assert any("neighbour" in text for text in outcome.observations)


def test_a_malformed_extra_is_skipped_and_the_batch_continues(tmp_path: Path) -> None:
    source = make_file(tmp_path, "a.py")
    other = make_file(tmp_path, "b.py")
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    reply = replace(
        read(source),
        extra_actions=("not json at all", json.dumps({"tool": "read_range"}), read(other).text),
    )
    llm = StubLlm(replies=[reply])

    run(state, llm, FakeVerify())

    assert tools_run(state) == ["read_range", "read_range"]
    assert batch_notes(state) == ["executed 1 of 3 extra calls"]


def test_an_extra_that_raises_does_not_end_the_run(tmp_path: Path, monkeypatch) -> None:
    # `_execute` is documented never to raise, and the primary path trusts that. A bonus
    # action is exactly where that trust is worth paying an except clause for.
    source = make_file(tmp_path, "a.py")
    other = make_file(tmp_path, "b.py")
    real = loop._execute

    def explode(tool, args, state, *, playbook):
        if str(args.get("path", "")) == str(other):
            raise RuntimeError("secret-looking detail")
        return real(tool, args, state, playbook=playbook)

    monkeypatch.setattr(loop, "_execute", explode)
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(replies=[batched(read(source), read(other))])

    outcome = run(state, llm, FakeVerify())

    assert outcome.stop_reason == "steps"
    failures = [
        event
        for event in state.audit
        if event.kind == "tool" and "RuntimeError" in event.detail
    ]
    assert len(failures) == 1
    assert all("secret-looking detail" not in event.detail for event in state.audit)


def test_the_tool_trace_marks_batch_actions(tmp_path: Path, monkeypatch, capsys) -> None:
    source = make_file(tmp_path, "a.py")
    other = make_file(tmp_path, "b.py")
    monkeypatch.setenv(limits.TRACE_TOOLS_ENV_NAME, "1")
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(replies=[batched(read(source), read(other))])

    run(state, llm, FakeVerify())

    printed = capsys.readouterr().out.splitlines()
    lines = [line for line in printed if line.startswith("agent tool:")]
    assert len(lines) == 2
    assert lines[0].startswith("agent tool: step=1 read_range")
    assert lines[1].startswith("agent tool: step=1.2 read_range")


def test_a_reply_without_extras_behaves_exactly_as_before(tmp_path: Path) -> None:
    source = make_file(tmp_path, "a.py")
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(replies=[read(source)])

    outcome = run(state, llm, FakeVerify())

    assert tools_run(state) == ["read_range"]
    assert batch_notes(state) == [], "no note on a step that carried no batch"
    assert all(not text.startswith("[step 1.") for text in outcome.observations)


# --- the two modules wired together -------------------------------------------


def test_a_real_multi_call_reply_reaches_the_loop_as_one_batched_step(tmp_path: Path) -> None:
    """The seam that was broken: `agent.llm` parses the batch, `agent.loop` runs it.

    Everything above this point hands the loop extras built by hand, so it proves the
    loop's half only. Here the reply is the wire format the stand actually returns --
    several `tool_calls` in one message -- and the assertion is that nothing between
    the socket and the workspace drops them.
    """
    files = [make_file(tmp_path, f"router_{index}.py") for index in range(3)]
    calls = [
        {
            "function": {
                "name": llm.TOOL_NAME,
                "arguments": json.dumps({"tool": "read_range", "path": str(path), "start_line": 1}),
            }
        }
        for path in files
    ]
    payload = json.dumps(
        {
            "choices": [{"message": {"content": None, "tool_calls": calls}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 12},
        }
    ).encode()

    client = llm.HttpLlm(
        settings=llm.LlmSettings.from_env(
            {
                "LOCAL_AGENT_MODEL": "small-local",
                "OPENAI_BASE_URL": "http://127.0.0.1:8000/v1/",
                "OPENAI_API_KEY": "sk-test-0123456789abcdef",
            }
        ),
        transport=lambda url, body, headers, timeout_s: (200, payload),
        sleep=lambda _: None,
    )
    state = make_state(tmp_path)
    state.budget.max_steps = 1

    outcome = run(state, client, FakeVerify())

    assert state.budget.steps_used == 1
    assert tools_run(state) == ["read_range"] * 3
    assert step_markers(outcome) == ["[step 1]", "[step 1.2]"]
    assert "[step 1.3]" in batch_entry(outcome)
    assert state.budget.total_tokens == 52, "one reply is one call, billed once"


def test_a_batch_does_not_blind_the_repeat_detectors(tmp_path: Path) -> None:
    # The critic's probe (16.09): a primary that fails the same way every step, with
    # four fresh reads riding along. Before the fix the reads filled the six-record
    # detector window and the failing primary was never seen twice in a row.
    files = [make_file(tmp_path, f"r{index}.py") for index in range(4)]
    missing = tmp_path / "does-not-exist.py"
    state = make_state(tmp_path)
    state.budget.max_steps = 8
    llm = StubLlm(replies=[batched(read(missing), *(read(path) for path in files))] * 8)

    outcome = run(state, llm, FakeVerify())

    # The repeat detector saw the failing primary twice in a row and refused the copies:
    # with extras in the window it never did (the critic's measured probe).
    refusals = [text for text in outcome.observations if "repeating it will not produce" in text]
    assert refusals, "the failing primary must be recognised as a repeat"
    assert all(record.tool == "read_range" for record in state.actions)
    assert len(state.actions) <= limits.ACTION_HISTORY
    assert tools_run(state).count("read_range") < 8 * 5, "refused copies do not run, nor do their extras"


def test_a_repeat_whose_result_left_the_history_runs_again(tmp_path: Path) -> None:
    # After a re-lay the model no longer sees the earlier read; skipping its request
    # for the same file silently left it with neither the content nor a word about why.
    files = [make_file(tmp_path, f"big_{index}.py", size=4000) for index in range(6)]
    state = make_state(tmp_path)
    state.budget.max_steps = 6
    llm = StubLlm(
        replies=[*(read(path) for path in files[:5]), batched(read(files[5]), read(files[0]))]
    )

    outcome = run(state, llm, FakeVerify())

    assert "[earlier observations omitted]" in str(llm.calls[-1]), "the fixture forces a re-lay"
    assert "big_0.py" not in str(llm.calls[-1]), "the first read is out of the model's view"
    assert batch_notes(state) == ["executed 1 of 1 extra calls"]
    assert "[step 6.2]" in batch_entry(outcome)


def test_a_skipped_repeat_is_named_in_the_batch_entry(tmp_path: Path) -> None:
    first = make_file(tmp_path, "a.py")
    second = make_file(tmp_path, "b.py")
    state = make_state(tmp_path)
    state.budget.max_steps = 2
    llm = StubLlm(replies=[read(first), batched(read(second), read(first))])

    outcome = run(state, llm, FakeVerify())

    assert "[step 2.2] read_range: same call as [step 1] above, not repeated" in outcome.observations[-1]
