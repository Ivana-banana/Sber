"""Шаги — связывающее ограничение только на быстром endpoint, и лечится один раз.

Измерено на стенде Qwen3.6-35B-A3B thinking (16.09, ~120 tok/s, вызов 2-4 с): эпизод
выбирает `MAX_STEPS` за 90-130 с и останавливается, когда из 600 с бюджета не потрачено
400+. Оба нуля кандидата 95ec07b на шести повторах закончились именно так (`stop=steps`),
и в провалившемся code-fix модель одиннадцать раз перечитала уязвимый обработчик и ни
разу не позвала `replace_text`.

Отсюда два поведения, которые описывают эти тесты, и оба обязаны быть узкими:

* подсказка на чекпоинте для code-fix говорит про правку (`replace_text`), а не про
  `write_file` — иначе она указывает на неверный инструмент;
* лимит шагов один раз поднимается, но только когда времени объективно много и прогон
  реально двигается. Медленный endpoint, короткий бюджет и прогон, ходящий по кругу,
  не получают ничего — это регрессионная защита, а не общий подъём лимита.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent import limits, loop, playbooks
from agent.ports import LlmError, LlmReply, Message
from agent.state import AgentState, Budget
from tests.stub_llm import StubLlm
from tests.test_loop import FakeVerify, action, make_brief, make_state, run


class Clock:
    """Monotonic time the test owns, so nothing here sleeps."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class TickingLlm:
    """A `StubLlm` that also charges wall clock for every call it answers.

    The stub replays a transcript instantly, which is exactly what makes the fast
    endpoint the default in these tests. A slow endpoint is that same transcript with
    seconds attached, and the difference between the two arms is this one number.
    """

    inner: StubLlm
    clock: Clock
    seconds: float
    calls: list[list[Message]] = field(default_factory=list)

    def complete(
        self,
        messages: list[Message],
        *,
        max_output_tokens: int,
        timeout_s: float | None = None,
    ) -> LlmReply | LlmError:
        self.clock.advance(self.seconds)
        self.calls.append(list(messages))
        return self.inner.complete(
            messages, max_output_tokens=max_output_tokens, timeout_s=timeout_s
        )

    def complete_structured(
        self,
        messages: list[Message],
        *,
        schema: dict[str, object],
        max_output_tokens: int,
        timeout_s: float | None = None,
    ) -> LlmReply | LlmError:
        self.clock.advance(self.seconds)
        self.calls.append(list(messages))
        return self.inner.complete_structured(
            messages,
            schema=schema,
            max_output_tokens=max_output_tokens,
            timeout_s=timeout_s,
        )


def writes(tmp_path: Path, count: int) -> StubLlm:
    """A model that keeps producing new files: progress by every observable measure."""
    return StubLlm(
        replies=[
            action("write_file", path=str(tmp_path / f"f{index}.txt"), content=f"{index}")
            for index in range(count)
        ]
    )


def extension_events(state: AgentState) -> list[str]:
    return [
        event.detail
        for event in state.audit
        if event.kind == "budget" and event.name == "steps-extended"
    ]


# --- the extension, as the loop grants it ------------------------------------


def test_the_cap_is_extended_once_when_time_and_progress_are_both_left(
    tmp_path: Path,
) -> None:
    """The measured case: thirty steps spent, four hundred seconds not spent."""
    cap = limits.MAX_STEPS + limits.STEP_EXTENSION
    state = make_state(tmp_path)

    outcome = run(state, writes(tmp_path, cap + 5), FakeVerify())

    assert state.budget.steps_extension_granted == limits.STEP_EXTENSION
    assert state.budget.max_steps == cap
    assert state.budget.steps_used == cap, "the raised cap ends the run like the old one"
    assert outcome.stop_reason == "steps", "the stop vocabulary the eval parsers read"


def test_the_extension_is_granted_exactly_once(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    run(state, writes(tmp_path, limits.MAX_STEPS + 2 * limits.STEP_EXTENSION + 5), FakeVerify())

    assert len(extension_events(state)) == 1
    assert state.budget.max_steps == limits.MAX_STEPS + limits.STEP_EXTENSION
    assert state.budget.extend_steps() == 0, "nothing grants a second one afterwards"


def test_the_extension_records_an_audit_event_with_its_reason(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    run(state, writes(tmp_path, limits.MAX_STEPS + limits.STEP_EXTENSION + 5), FakeVerify())

    detail = extension_events(state)[0]
    assert f"+{limits.STEP_EXTENSION} steps" in detail
    assert "spendable" in detail, "the reason names the fact that justified the grant"
    event = next(item for item in state.audit if item.name == "steps-extended")
    assert event.step == limits.MAX_STEPS, "recorded at the cap it was granted at"


def test_the_extension_note_is_mirrored_under_the_budget_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Same switch as every other budget note: a run that silently awarded itself more
    # steps is indistinguishable from outside from one that never hit the cap.
    monkeypatch.setenv(limits.TRACE_BUDGET_ENV_NAME, "1")
    state = make_state(tmp_path)

    run(state, writes(tmp_path, limits.MAX_STEPS + limits.STEP_EXTENSION + 5), FakeVerify())

    printed = capsys.readouterr().out
    assert f"agent budget: step={limits.MAX_STEPS} steps-extended" in printed


def test_a_run_that_used_the_extension_says_so_in_its_summary(tmp_path: Path) -> None:
    # `stop_reason` stays `steps`, because eval parsers read that vocabulary; the fact
    # that the cap was the raised one travels in the free-text summary.
    state = make_state(tmp_path)

    outcome = run(
        state, writes(tmp_path, limits.MAX_STEPS + limits.STEP_EXTENSION + 5), FakeVerify()
    )

    assert outcome.stop_reason == "steps"
    assert outcome.summary.startswith("steps (extended once")
    assert f"+{limits.STEP_EXTENSION} steps" in outcome.summary


# --- and the three runs that must not get it ---------------------------------


def test_a_slow_endpoint_reaches_the_cap_with_no_time_to_spare_and_gets_nothing(
    tmp_path: Path,
) -> None:
    """The fast-path regression guard.

    Twelve seconds a call is the slow arm: thirty steps then cost 360 s of a 600 s
    budget, less than half of it is still spendable, and nothing about the run changes.
    """
    clock = Clock()
    state = make_state(tmp_path, clock=clock)
    llm = TickingLlm(writes(tmp_path, limits.MAX_STEPS + limits.STEP_EXTENSION + 5), clock, 12.0)

    outcome = run(state, llm, FakeVerify())  # type: ignore[arg-type]

    spare = state.budget.total_s * limits.STEP_EXTENSION_MIN_SPARE_FRACTION
    assert state.budget.spendable_s() < spare
    assert state.budget.steps_used == limits.MAX_STEPS
    assert extension_events(state) == []
    assert outcome.stop_reason == "steps"
    assert "extended" not in outcome.summary


def test_a_run_that_only_re_reads_what_it_has_read_gets_nothing(tmp_path: Path) -> None:
    """The loop this extension must not feed.

    The failed code-fix episode: `read_range` of the same handler eleven times, no
    `replace_text` ever issued. Those observations are ones the run has already seen and
    they change no file, so the window says the run is standing still -- and time left
    on the clock is not a reason to buy more laps of it.
    """
    source = tmp_path / "app.py"
    source.write_text("def login(name):\n    return query(f\"u = '{name}'\")\n", encoding="utf-8")
    state = make_state(tmp_path)
    llm = StubLlm(
        replies=[
            action("read_range", path=str(source), start_line=1)
            for _ in range(limits.MAX_STEPS)
        ]
    )

    outcome = run(state, llm, FakeVerify(), playbook=playbooks.CODE_FIX)

    spare = state.budget.total_s * limits.STEP_EXTENSION_MIN_SPARE_FRACTION
    assert state.budget.spendable_s() > spare, "the clock alone would have allowed it"
    assert state.shows_progress() is False, "no state change, and nothing new last step"
    assert extension_events(state) == []
    assert state.budget.steps_used == limits.MAX_STEPS
    assert outcome.stop_reason == "steps"


def test_a_cap_somebody_lowered_on_purpose_is_never_raised(tmp_path: Path) -> None:
    # The extension answers one measured situation -- the default cap arriving in 90-130 s
    # -- and a caller that asked for a three-step run asked for a three-step run.
    state = make_state(tmp_path)
    state.budget.max_steps = 3

    run(state, writes(tmp_path, 10), FakeVerify())

    assert state.budget.steps_used == 3
    assert extension_events(state) == []


# --- what the budget itself promises -----------------------------------------


def make_capped_budget(total_s: float = 600.0) -> tuple[Budget, Clock]:
    """A budget standing exactly at its step cap, which is where the question arises."""
    clock = Clock()
    budget = Budget(total_s=total_s, clock=clock)
    for _ in range(limits.MAX_STEPS):
        budget.start_step()
    return budget, clock


def test_may_act_is_false_again_once_the_extension_is_spent() -> None:
    budget, _ = make_capped_budget()
    assert not budget.may_act()

    assert budget.extend_steps() == limits.STEP_EXTENSION
    assert budget.may_act(), "the grant is what the extra steps are for"

    for _ in range(limits.STEP_EXTENSION):
        assert budget.may_act()
        budget.start_step()

    assert not budget.may_act()
    assert budget.extend_steps() == 0
    assert not budget.may_act(), "one grant per run, and then the cap ends it"


def test_a_stuck_run_needs_both_answers_before_anything_is_granted(tmp_path: Path) -> None:
    # The budget answers only the clock half; the loop refuses the grant on the other.
    state = make_state(tmp_path)
    for _ in range(limits.MAX_STEPS):
        state.budget.start_step()

    assert state.budget.may_extend_steps() is True
    assert state.shows_progress() is False, "an empty history is not evidence of progress"
    assert loop._extend_steps(state) is False
    assert state.budget.max_steps == limits.MAX_STEPS


def test_progress_is_an_accepted_edit_in_the_window(tmp_path: Path) -> None:
    # Novel reads and re-run tests are not progress (critic, 16.09): thirty different
    # windows of one file are each novel, and pytest prints a fresh timing every time.
    state = make_state(tmp_path)
    args = {"path": str(tmp_path / "app.py"), "start_line": 1}

    state.observe("read_range", args, "line one")
    assert state.shows_progress() is False, "a new read is not an edit"

    state.advance_state_epoch()
    state.observe("run_shell", {"command": "python -m pytest -q"}, "1 failed in 0.17s")
    assert state.shows_progress() is False, "a re-run test suite is not an edit"

    state.advance_state_epoch()
    state.observe("write_file", {"path": str(tmp_path / "out.txt")}, "wrote 3 bytes")
    assert state.shows_progress() is True, "an accepted edit landed in the window"

    state.observe("replace_text", {"path": str(tmp_path / "app.py")}, "replaced", ok=False)
    assert state.shows_progress() is True, "the earlier accepted edit still counts inside the window"


def test_an_old_success_is_not_evidence_that_the_run_is_still_moving(
    tmp_path: Path,
) -> None:
    # Refused actions, malformed replies and endpoint errors leave no record at all, so
    # a window full of them looks exactly like the window before them. Steps are what
    # tell the two apart, and the run that has executed nothing for a whole window of
    # them is not the one this extension was measured on.
    state = make_state(tmp_path)
    state.budget.start_step()
    state.observe("write_file", {"path": str(tmp_path / "out.txt")}, "wrote 3 bytes")
    assert state.shows_progress() is True

    for _ in range(limits.ACTION_HISTORY):
        state.budget.start_step()

    assert state.shows_progress() is False


def test_progress_looks_no_further_back_than_the_detector_window(tmp_path: Path) -> None:
    # The window is what the detectors act on, and it is deliberately short: an edit six
    # actions ago followed by five identical reads is the episode this refuses to extend.
    state = make_state(tmp_path)
    state.advance_state_epoch()
    state.observe("write_file", {"path": str(tmp_path / "out.txt")}, "wrote 3 bytes")
    for _ in range(limits.ACTION_HISTORY):
        state.observe("read_range", {"path": str(tmp_path / "app.py")}, "same window")

    assert len(state.actions) == limits.ACTION_HISTORY
    assert state.shows_progress() is False


# --- and the two bounds it may not move --------------------------------------


def test_the_early_checkpoint_keys_off_the_cap_before_the_extension() -> None:
    """Producing starts at 0.4 of the *original* cap, whatever the cap becomes.

    Keying it off `max_steps` would push the checkpoint from step 12 to step 18 for
    exactly the runs that reached the cap -- moving a deadline later as a side effect of
    granting more time is how a deliverable gets lost.
    """
    budget, _ = make_capped_budget()
    budget.extend_steps()

    assert budget.base_max_steps == limits.MAX_STEPS
    budget.steps_used = int(limits.MAX_STEPS * limits.EARLY_DELIVERABLE_STEP_FRACTION)
    assert budget.past_early_deadline(), "the checkpoint did not move when the cap did"


def test_the_extension_never_reaches_into_the_verifier_reserve() -> None:
    budget, clock = make_capped_budget()
    reserve = budget.verify_reserve_s
    budget.extend_steps()

    clock.advance(600.0 - reserve)

    assert budget.verify_reserve_s == reserve, "the reserve is not what pays for the steps"
    assert not budget.may_act(), "extra steps are still bounded by the clock"
    assert budget.remaining_s() > 0, "and the verifier still has its seconds"


def test_a_short_task_budget_never_earns_the_extension() -> None:
    """On a 120 s task the reserve alone is 30 s, so this asks for 60 s of spendable
    time at step 30. A call costs 2-4 s on the fastest stand measured, so thirty steps
    cannot cost less than about a minute and the threshold is never met."""
    budget, clock = make_capped_budget(total_s=120.0)
    clock.advance(31.0)

    assert not budget.may_extend_steps()
    assert budget.extend_steps() == 0
    assert budget.max_steps == limits.MAX_STEPS


def test_the_extension_is_refused_once_only_the_reserve_is_left() -> None:
    budget, clock = make_capped_budget()
    clock.advance(600.0 - budget.verify_reserve_s)

    assert budget.spendable_s() <= 0
    assert not budget.may_extend_steps()


# --- the checkpoint hint for a code-fix --------------------------------------


def checkpoints(outcome: loop.Outcome) -> list[str]:
    return [text for text in outcome.observations if "[бюджет]" in text]


def code_fix_run(tmp_path: Path) -> loop.Outcome:
    """A grounded code-fix run standing past the early checkpoint with nothing written."""
    source = tmp_path / "app.py"
    source.write_text("def login(name):\n    return query(f\"u = '{name}'\")\n", encoding="utf-8")
    clock = Clock()
    state = make_state(tmp_path, clock=clock)
    llm = StubLlm(replies=[action("read_range", path=str(source), start_line=1) for _ in range(3)])
    clock.now = limits.EARLY_DELIVERABLE_DEADLINE_S + 1

    return loop.run(
        state,
        llm,
        FakeVerify(),
        brief=make_brief(tmp_path),
        playbook=playbooks.CODE_FIX,
    )


def test_the_code_fix_checkpoint_asks_for_the_edit(tmp_path: Path) -> None:
    """P-L2: for a code-fix the best attempt is the edit, not a file.

    The measured episode had located and re-read the vulnerable handler eleven times and
    never issued `replace_text`; a hint that says "write your best attempt with
    write_file now" points at the wrong tool there.
    """
    checkpoint = checkpoints(code_fix_run(tmp_path))[-1]

    assert "replace_text" in checkpoint
    assert "write_file" not in checkpoint, "the hint may not point at the wrong tool"


def test_the_code_fix_checkpoint_carries_the_anchor_and_the_restart(tmp_path: Path) -> None:
    # The two things 5 of 5 failed edits got wrong: an anchor that stopped short of the
    # call consuming the value, and a service still running the old code.
    checkpoint = checkpoints(code_fix_run(tmp_path))[-1]

    assert "якор" in checkpoint
    assert "вызовом" in checkpoint
    assert "entrypoint" in checkpoint
    assert "репро" in checkpoint


def test_the_code_fix_checkpoint_stops_the_re_reading(tmp_path: Path) -> None:
    checkpoint = checkpoints(code_fix_run(tmp_path))[-1]

    assert "не перечитывай" in checkpoint.lower()
    assert "прочитан" in checkpoint


def test_the_code_fix_checkpoint_waits_until_something_has_been_observed(
    tmp_path: Path,
) -> None:
    # Before the first successful observation there is no located statement to edit, and
    # the run gets the "look first" hint it has always got.
    first = checkpoints(code_fix_run(tmp_path))[0]

    assert "ни одного успешного наблюдения" in first
    assert "replace_text" not in first


def test_the_checkpoint_of_the_other_playbooks_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / "evidence.log").write_text("first line\n", encoding="utf-8")
    clock = Clock()
    state = make_state(tmp_path, clock=clock)
    llm = StubLlm(replies=[action("list_dir", path=str(tmp_path)) for _ in range(3)])
    clock.now = limits.EARLY_DELIVERABLE_DEADLINE_S + 1

    outcome = run(state, llm, FakeVerify())

    assert "write_file" in checkpoints(outcome)[-1]
    assert "replace_text" not in checkpoints(outcome)[-1]
