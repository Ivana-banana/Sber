"""Что петля спрашивает у модели после ответа, в котором не было действия.

Измерено на стенде Qwen3.6-35B-A3B (thinking, ollama игнорирует `think:false`), трейсы
вызовов 16.09 под `build/`: к середине прогона запрос вырастает до ~6.9k токенов, и
модель начинает отвечать рассуждением без единого вызова инструмента — четыре логических
вызова подряд возвращают `completion_tokens` ровно по поднятому потолку (8192), пустой
`content`, ни одного `tool_calls`, примерно по 70 с каждый. Клиенту здесь уже нечего
пробовать: бюджет поднят, `enable_thinking=false` этот сервер игнорирует. Петля тратила
три подряд идущих отказа и заканчивала прогон как `llm-unusable` — при готовом, но
непроверенном результате на диске, то есть на нуле.

Эти тесты описывают единственный доступный ход: не повторять тот же запрос, а послать
следующий в «фокусном» виде — тот же системный промпт и задача, история сжата до
`limits.FOCUS_RELAY_KEEP` наблюдений, и последней строкой одна просьба вернуть вызов
инструмента без рассуждений. Ровно на один вызов. И столько же отказов, сколько раньше:
фокус занимает место второго из трёх, а не добавляет четвёртый.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from agent import limits, llm as llm_module, loop, playbooks, prompt
from agent.ports import LlmError, LlmReply, Message
from tests.stub_llm import StubLlm
from tests.test_llm import ENV
from tests.test_loop import FakeVerify, action, make_brief, make_state, run


def runaway() -> LlmError:
    """The reply `agent.llm` hands back after a reasoning runaway.

    Built here from the typed contract rather than copied from a log: `retryable=False`
    because the client has already spent its ladder on it, the output tokens because the
    generation was billed, and the flag because that is the only thing the loop is
    allowed to read. The reason text is realistic and deliberately never asserted on.
    """
    return LlmError(
        reason="reply truncated by length at style native_tools",
        retryable=False,
        output_tokens=limits.LLM_THINKING_MAX_OUTPUT_TOKENS,
        truncated_reasoning=True,
    )


@dataclass
class CapRecorder(StubLlm):
    """A transcript player that also remembers the output ceiling it was handed."""

    caps: list[int] = field(default_factory=list)

    def complete_structured(
        self,
        messages: list[Message],
        *,
        schema: dict[str, object],
        max_output_tokens: int,
        timeout_s: float | None = None,
    ) -> LlmReply | LlmError:
        self.caps.append(max_output_tokens)
        return super().complete_structured(
            messages, schema=schema, max_output_tokens=max_output_tokens, timeout_s=timeout_s
        )


def texts(messages: list[Message]) -> list[str]:
    return [message["content"] for message in messages]


def focused(messages: list[Message]) -> bool:
    return prompt.FOCUS_NOTICE in texts(messages)


def relayed(messages: list[Message]) -> list[str]:
    """The observations a focused request carried, without its scaffolding."""
    return [
        text
        for text in texts(messages)[1:]
        if text not in {prompt.OMISSION_NOTICE, prompt.FOCUS_NOTICE}
    ]


# --- the request that changes -------------------------------------------------


def test_a_runaway_narrows_the_next_request_instead_of_repeating_it(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    llm = StubLlm(
        replies=[
            action("list_dir", path=str(tmp_path)),
            action("list_dir", path=str(tmp_path)),
            runaway(),
            action("write_file", path=str(target), content="x"),
            action("finish", path=str(target), verify="test -s out.txt", summary="done"),
        ]
    )

    outcome = run(make_state(tmp_path), llm, FakeVerify((True, "command", "")))

    assert outcome.stop_reason == "verified"
    before, after = llm.calls[2], llm.calls[3]
    assert focused(after) and not focused(before)
    # The contract, in three parts: the system prompt is the same object it always was,
    # the history is cut to the window, and the ask is the last thing the model reads.
    assert after[0] == before[0], "the system prompt and the task are untouched"
    assert len(relayed(after)) == limits.FOCUS_RELAY_KEEP
    # What the window is worth spending on, and why it is two and not one: the runaway
    # appends its own error line, so the newest observation is that note, and the one
    # behind it is the result of the last action the model actually took.
    assert relayed(after)[0] == relayed(before)[-1], "the last real observation survives"
    assert "endpoint error" in relayed(after)[-1]
    assert texts(after)[-1] == prompt.FOCUS_NOTICE


def test_a_focused_request_is_the_smaller_half_of_the_one_that_ran_away(
    tmp_path: Path,
) -> None:
    """The size that is the whole point, measured rather than asserted structurally.

    The measured runaway sat at ~6.9k prompt tokens, most of it relayed observations.
    Three `read_range` windows of a wide file are the same shape here: the request the
    model stopped answering is ~27k characters and the focused one is ~11k, with the
    system prompt and the task -- which the model needs and which it is not choking on --
    untouched inside it.
    """
    big = tmp_path / "wide.txt"
    big.write_text("".join(f"line {n:04d} " + "z" * 60 + "\n" for n in range(400)), encoding="utf-8")
    llm = StubLlm(
        replies=[
            action("read_range", path=str(big), start_line=1),
            action("read_range", path=str(big), start_line=120),
            action("read_range", path=str(big), start_line=240),
            runaway(),
            action("list_dir", path=str(tmp_path)),
        ]
    )

    run(make_state(tmp_path), llm, FakeVerify())

    ran_away, focus = llm.calls[3], llm.calls[4]
    assert focused(focus) and not focused(ran_away)
    assert sum(len(text) for text in texts(focus)) < sum(len(text) for text in texts(ran_away)) / 2
    assert texts(focus)[0] == texts(ran_away)[0], "the system prompt is not what is cut"


def test_focus_lasts_exactly_one_call(tmp_path: Path) -> None:
    """A delivered focused reply ends the focus; it is not a mode the run stays in."""
    target = tmp_path / "out.txt"
    llm = StubLlm(
        replies=[
            action("list_dir", path=str(tmp_path)),
            runaway(),
            action("write_file", path=str(target), content="x"),
            action("finish", path=str(target), verify="test -s out.txt", summary="done"),
        ]
    )

    outcome = run(make_state(tmp_path), llm, FakeVerify((True, "command", "")))

    assert outcome.stop_reason == "verified"
    assert [focused(call) for call in llm.calls] == [False, False, True, False]


def test_the_request_after_the_focused_one_is_the_one_that_would_have_been_sent(
    tmp_path: Path,
) -> None:
    """Focus does not move the anchor, so it costs one broken prefix and not a re-lay.

    The call after it is built from the same base and the same history as always: the
    server pays a prefill for the focused call and one for the call behind it, and then
    the run is extending requests again exactly as before.
    """
    target = tmp_path / "out.txt"
    llm = StubLlm(
        replies=[
            action("list_dir", path=str(tmp_path)),
            runaway(),
            action("list_dir", path=str(tmp_path)),
            action("write_file", path=str(target), content="x"),
            action("finish", path=str(target), verify="test -s out.txt", summary="done"),
        ]
    )
    state = make_state(tmp_path)

    outcome = run(state, llm, FakeVerify((True, "command", "")))

    assert outcome.stop_reason == "verified"
    normal_before, normal_after = llm.calls[1], llm.calls[3]
    assert not focused(normal_after)
    assert texts(normal_after)[: len(texts(normal_before))] == texts(normal_before), (
        "the ordinary requests on either side of the focus still extend one another"
    )


def test_the_focused_call_keeps_the_same_output_ceiling(tmp_path: Path) -> None:
    """The prompt ran away, not the room to answer in.

    The raised ceiling lives in `agent.llm` and is sticky, so the loop's job here is to
    leave it alone: asking for less would truncate the answer we are trying to get, and
    asking for more would buy a longer runaway.
    """
    target = tmp_path / "out.txt"
    llm = CapRecorder(
        replies=[
            runaway(),
            action("write_file", path=str(target), content="x"),
            action("finish", path=str(target), verify="test -s out.txt", summary="done"),
        ]
    )

    run(make_state(tmp_path), llm, FakeVerify((True, "command", "")))

    assert focused(llm.calls[1])
    assert len(llm.caps) == 3, "the runaway, the focused retry and the finish"
    assert llm.caps == [limits.LLM_MAX_OUTPUT_TOKENS] * 3


# --- failure accounting: fewer zeros, never more calls ------------------------


def test_a_delivered_focused_reply_resets_the_failure_counter(tmp_path: Path) -> None:
    """Three runaways in one run, each answered, and the run still finishes.

    Without the reset this transcript is `llm-unusable` at the third one -- which is the
    measured zero this whole change exists to remove.
    """
    target = tmp_path / "out.txt"
    llm = StubLlm(
        replies=[
            runaway(),
            action("list_dir", path=str(tmp_path)),
            runaway(),
            action("list_dir", path=str(tmp_path)),
            runaway(),
            action("write_file", path=str(target), content="x"),
            action("finish", path=str(target), verify="test -s out.txt", summary="done"),
        ]
    )

    outcome = run(make_state(tmp_path), llm, FakeVerify((True, "command", "")))

    assert outcome.solved is True
    assert outcome.stop_reason == "verified"
    assert [focused(call) for call in llm.calls].count(True) == 3, "one focus per runaway"


def test_two_runaways_in_a_row_still_end_at_the_existing_threshold(tmp_path: Path) -> None:
    """A runaway is a hard failure, and a focused runaway is the next one. No discount.

    Worst case is therefore exactly today's worst case: three logical calls, the second
    of them the focused one. On the measured stand that is 4 endpoint calls of ~70 s --
    the first logical call pays two, the raised retry and the thinking-off retry -- so
    ~280 s, unchanged. What the seconds buy is a request the model has not already
    failed to answer.
    """
    state = make_state(tmp_path)
    llm = StubLlm(replies=[runaway(), runaway(), runaway(), runaway()])

    outcome = run(state, llm, FakeVerify())

    assert outcome.stop_reason == "llm-unusable"
    assert len(llm.calls) == limits.MAX_CONSECUTIVE_LLM_FAILURES
    assert [focused(call) for call in llm.calls] == [False, True, False]


def test_a_focused_runaway_does_not_ask_for_another_focus(tmp_path: Path) -> None:
    """No infinite focus: the flag is consumed by the call that carries it."""
    target = tmp_path / "out.txt"
    llm = StubLlm(
        replies=[
            runaway(),
            runaway(),
            action("write_file", path=str(target), content="x"),
            action("finish", path=str(target), verify="test -s out.txt", summary="done"),
        ]
    )

    outcome = run(make_state(tmp_path), llm, FakeVerify((True, "command", "")))

    assert outcome.stop_reason == "verified"
    assert [focused(call) for call in llm.calls] == [False, True, False, False]


def test_a_truncation_that_is_not_a_runaway_asks_the_same_question_again(
    tmp_path: Path,
) -> None:
    """The client's verdict, and only the client's verdict.

    A cutoff with an ordinary answer behind it is a model that was still talking to us
    (`F4-7b-regression-4080`): it needs room, or it is looping, and a narrower prompt is
    no answer to either. The reason text reads identically, which is exactly why the
    loop must not read it.
    """
    target = tmp_path / "out.txt"
    llm = StubLlm(
        replies=[
            LlmError("reply truncated by length at style native_tools", retryable=False),
            action("write_file", path=str(target), content="x"),
            action("finish", path=str(target), verify="test -s out.txt", summary="done"),
        ]
    )

    run(make_state(tmp_path), llm, FakeVerify((True, "command", "")))

    assert not any(focused(call) for call in llm.calls)


# --- observability -------------------------------------------------------------


def test_the_focused_retry_is_audited_as_a_budget_note(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    llm = StubLlm(replies=[runaway(), runaway(), runaway()])

    run(state, llm, FakeVerify())

    notes = [(event.kind, event.name, event.detail) for event in state.audit]
    assert ("budget", "focus", "focused retry after a truncated reasoning reply") in notes
    assert [note for note in notes if note[1] == "focus"] == [
        ("budget", "focus", "focused retry after a truncated reasoning reply")
    ], "one note per focus cycle, like the calls it stands for"


def test_no_focus_note_is_written_for_a_call_that_never_happens(tmp_path: Path) -> None:
    # The note is written where the focused request is built, not where it is decided:
    # the clock or the step cap can end the run in between, and a trace that claims a
    # retry nobody sent is worse than no trace.
    state = make_state(tmp_path)
    state.budget.max_steps = 1
    llm = StubLlm(replies=[runaway()])

    run(state, llm, FakeVerify())

    assert len(llm.calls) == 1
    assert not [event for event in state.audit if event.name == "focus"]


def test_the_focus_note_is_mirrored_by_the_budget_trace(tmp_path: Path, capsys, monkeypatch) -> None:
    # Same switch as the other notes that explain a run's wall clock: a live run that
    # spends 70 s reasoning looks from outside exactly like one that is working.
    monkeypatch.setenv(limits.TRACE_BUDGET_ENV_NAME, "1")
    llm = StubLlm(replies=[runaway(), runaway(), runaway()])

    run(make_state(tmp_path), llm, FakeVerify())

    assert "focused retry after a truncated reasoning reply" in capsys.readouterr().out


def test_the_focus_line_is_a_request_and_never_an_observation(tmp_path: Path) -> None:
    """History stays clean: the only thing the runaway adds to it is the error itself."""
    target = tmp_path / "out.txt"
    llm = StubLlm(
        replies=[
            runaway(),
            action("write_file", path=str(target), content="x"),
            action("finish", path=str(target), verify="test -s out.txt", summary="done"),
        ]
    )

    outcome = run(make_state(tmp_path), llm, FakeVerify((True, "command", "")))

    assert all(prompt.FOCUS_NOTICE not in text for text in outcome.observations)
    assert any("endpoint error" in text for text in outcome.observations), (
        "the failed call is still reported, never silently swallowed"
    )


# --- the fast path, and the neighbours ----------------------------------------


def test_a_run_without_a_runaway_sends_byte_identical_requests(tmp_path: Path) -> None:
    """An endpoint that never truncates pays nothing for any of this.

    Rebuilt against `prompt.build_messages` from the observations the run produced, so
    this compares the actual requests with the requests the loop sent before focus mode
    existed, message by message.
    """
    target = tmp_path / "out.txt"
    llm = StubLlm(
        replies=[
            action("list_dir", path=str(tmp_path)),
            action("list_dir", path=str(tmp_path)),
            action("write_file", path=str(target), content="x"),
            action("finish", path=str(target), verify="test -s out.txt", summary="done"),
        ]
    )
    state = make_state(tmp_path)

    outcome = run(state, llm, FakeVerify((True, "command", "")))

    assert outcome.stop_reason == "verified"
    # The transcript is chosen so that every step adds exactly one observation -- no
    # checkpoint hint, no refusal, no batch -- which is what makes replaying the history
    # alongside the calls legitimate.
    system = prompt.build_system(make_brief(tmp_path), playbooks.FORENSICS, state.instruction)
    history: list[str] = []
    base = 0
    for index, sent in enumerate(llm.calls):
        base = prompt.history_base(history, base)
        assert sent == prompt.build_messages(system, history, base), f"call {index} differs"
        if index < len(outcome.observations):
            history.append(outcome.observations[index])
    assert history, "the run has to have produced observations for this to mean anything"


def test_the_extras_of_a_focused_reply_are_still_a_batch(tmp_path: Path) -> None:
    """The L1 contract is untouched: focus changes the question, not what an answer may be.

    A model told to answer with one short call may still answer with several read-only
    ones, and there is no reason to throw the rest away -- that is the behaviour the
    batch exists for.
    """
    (tmp_path / "sub").mkdir()
    target = tmp_path / "out.txt"
    extra = action("list_dir", path=str(tmp_path / "sub"))
    llm = StubLlm(
        replies=[
            runaway(),
            replace(action("list_dir", path=str(tmp_path)), extra_actions=(extra.text,)),
            action("write_file", path=str(target), content="x"),
            action("finish", path=str(target), verify="test -s out.txt", summary="done"),
        ]
    )

    outcome = run(make_state(tmp_path), llm, FakeVerify((True, "command", "")))

    assert outcome.stop_reason == "verified"
    assert focused(llm.calls[1])
    # The extra's own target, not just the word: the primary `list_dir` lists `sub` as an
    # entry, so a looser assertion would pass with the batch thrown away.
    assert any(f"source={tmp_path / 'sub'}" in text for text in outcome.observations), (
        "the second read-only call of the focused reply still ran"
    )


def test_a_checkpoint_hint_and_the_focus_line_travel_together_in_that_order(
    tmp_path: Path,
) -> None:
    """Both can fire on the same step, and neither replaces the other -- nor the evidence.

    The checkpoint says what is worth writing now; the focus line says how to answer at
    all. The second is useless without the first, so the checkpoint arrives as history
    and the focus line closes the request.

    The regression underneath (review, 16.09): a runaway appends its own error line and
    the checkpoint prepends a hint, so a window counted in observations relayed exactly
    those two and dropped every observed fact. The request then asked a model that must
    not reason to write its best answer with nothing to write it from, which is the
    fabrication `agent.loop`'s `grounded` guard exists to prevent.
    """
    evidence = tmp_path / "evidence.log"
    evidence.write_text("2026-09-16 10:00:00 login failed from 10.0.0.7\n" * 3, encoding="utf-8")
    state = make_state(tmp_path)
    state.budget.early_deadline_s = 0.0  # every step is past the checkpoint
    llm = StubLlm(
        replies=[
            action("read_range", path=str(evidence), start_line=1),
            runaway(),
            action("list_dir", path=str(tmp_path)),
        ]
    )

    run(state, llm, FakeVerify())

    sent = texts(llm.calls[2])
    assert prompt.FOCUS_NOTICE == sent[-1], "the focus line is read last"
    assert any("10.0.0.7" in text for text in sent), "the observed fact survived the narrowing"
    checkpoints = [index for index, text in enumerate(sent) if text.startswith("[бюджет]")]
    assert checkpoints, "the checkpoint hint survived the narrowing"
    assert max(checkpoints) < len(sent) - 1


def test_a_batch_in_the_step_before_a_runaway_does_not_evict_the_evidence(
    tmp_path: Path,
) -> None:
    """The other half of the same finding, from the L1 side.

    A reply with extra read-only calls adds a second history entry for one step, so the
    newest two observations were the batch's entry and the runaway's error line -- and
    the read the run actually needed was already out of the window. A refused `finish`, a
    detector signal and a red verifier all append a second entry the same way.
    """
    (tmp_path / "empty").mkdir()
    evidence = tmp_path / "evidence.log"
    evidence.write_text("2026-09-16 10:00:00 login failed from 10.0.0.7\n" * 3, encoding="utf-8")
    extra = action("list_dir", path=str(tmp_path / "empty"))
    llm = StubLlm(
        replies=[
            replace(
                action("read_range", path=str(evidence), start_line=1),
                extra_actions=(extra.text,),
            ),
            runaway(),
            action("list_dir", path=str(tmp_path)),
        ]
    )

    run(make_state(tmp_path), llm, FakeVerify())

    sent = texts(llm.calls[2])
    assert prompt.FOCUS_NOTICE == sent[-1]
    assert any("10.0.0.7" in text for text in sent), "the primary read survived its own batch"


def test_focus_is_the_loops_own_and_the_macro_solver_is_untouched() -> None:
    """`agent.macro` builds one hand-made message list with no history to narrow.

    Recorded as a test because the flag now travels on every `LlmError` and the macro
    path reads none of them: nothing there is allowed to grow a second focus mode by
    accident.
    """
    from agent import macro

    source = Path(macro.__file__).read_text(encoding="utf-8")

    assert "FOCUS_NOTICE" not in source
    assert "FOCUS_RELAY_KEEP" not in source
    assert "focus_base" not in source
    assert "truncated_reasoning" not in source


def test_the_real_client_and_the_loop_agree_about_a_runaway(tmp_path: Path) -> None:
    """The whole chain against a fake endpoint that only ever runs away.

    Every other test here hands the loop a hand-built `LlmError`; this one proves the
    flag is actually produced. The endpoint answers every request the way the 35B stand
    did -- a reasoning field, empty content, `finish_reason=length`, and exactly the
    requested ceiling billed -- so the client walks its whole ladder, finds nothing left
    to try, and the loop gets the verdict rather than a sentence about it.

    The count is the cost analysis: three logical calls, which is
    `MAX_CONSECUTIVE_LLM_FAILURES`, and five endpoint requests. Three of those belong to
    the first logical call (the default ceiling, the raised retry, the thinking-off
    retry) and one each to the two after it, because both of those sticky flags are now
    set. The run ends where it ended before this change, having asked one question it
    had not already failed to answer. At the ~70 s a call measured on that stand, this
    is ~350 s either way: focus buys its chance for nothing.
    """
    sent: list[dict[str, object]] = []

    def endpoint(url: str, body: bytes, headers: dict[str, str], timeout_s: float):
        request = json.loads(body)
        sent.append(request)
        reply = {
            "choices": [
                {
                    "message": {"content": "", "reasoning": "thinking and never stopping"},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 1500, "completion_tokens": request["max_tokens"]},
        }
        return 200, json.dumps(reply).encode()

    client = llm_module.HttpLlm(
        settings=llm_module.LlmSettings.from_env(ENV),
        transport=endpoint,
        sleep=lambda _: None,
    )
    state = make_state(tmp_path)

    outcome = loop.run(
        state,
        client,
        FakeVerify(),
        brief=make_brief(tmp_path),
        playbook=playbooks.FORENSICS,
    )

    assert outcome.stop_reason == "llm-unusable"
    assert len(sent) == 5, "the endpoint requests the three logical calls cost"
    assert [note.name for note in state.audit if note.kind == "budget"].count("focus") == 1
    # The second logical call is the focused one: requests 4 and 5 are the two calls
    # after the first, and request 4 is the one that carries the line.
    assert prompt.FOCUS_NOTICE in [m["content"] for m in sent[3]["messages"]]
    assert prompt.FOCUS_NOTICE not in [m["content"] for m in sent[4]["messages"]]
    assert sent[3]["max_tokens"] == limits.LLM_THINKING_MAX_OUTPUT_TOKENS, "the room is kept"


def test_loop_reads_the_typed_flag_and_never_the_reason_text() -> None:
    """A reason text can quote the endpoint's own body. The flag is the contract.

    Read off the syntax tree and not the file, so that the phrase may appear in a comment
    or a docstring -- it has to, to explain any of this -- while a string constant the
    code could compare against would fail.
    """
    tree = ast.parse(Path(loop.__file__).read_text(encoding="utf-8"))
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]

    assert "truncated_reasoning" in {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not [text for text in literals if "truncated by length" in text]
