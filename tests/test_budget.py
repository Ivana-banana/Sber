"""The budget must hold without sleeping, so the clock is injected everywhere."""

from __future__ import annotations

from agent import limits
from agent.ports import LlmReply
from agent.state import Budget


class FakeClock:
    """Monotonic time we control. Same trick as the rate limiter in ai-v-massy."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_budget(total_s: float = 600.0) -> tuple[Budget, FakeClock]:
    clock = FakeClock()
    return Budget(total_s=total_s, clock=clock), clock


def test_reserve_is_a_fraction_within_clamps() -> None:
    budget, _ = make_budget(600.0)
    assert budget.verify_reserve_s == 600.0 * limits.VERIFY_RESERVE_FRACTION


def test_reserve_never_below_minimum() -> None:
    budget, _ = make_budget(60.0)
    assert budget.verify_reserve_s == limits.VERIFY_RESERVE_MIN_S


def test_reserve_never_above_maximum() -> None:
    budget, _ = make_budget(3600.0)
    assert budget.verify_reserve_s == limits.VERIFY_RESERVE_MAX_S


def test_reserve_cannot_exceed_the_whole_budget() -> None:
    budget, _ = make_budget(10.0)
    assert budget.verify_reserve_s == 10.0
    assert budget.spendable_s() == 0.0
    assert not budget.may_act()


def test_acting_stops_before_the_verifier_reserve() -> None:
    budget, clock = make_budget(600.0)
    reserve = budget.verify_reserve_s

    clock.advance(600.0 - reserve - 1.0)
    assert budget.may_act()

    clock.advance(2.0)
    assert not budget.may_act(), "the reserve belongs to the verifier, not to the loop"
    assert budget.remaining_s() > 0, "there is still wall clock left for verification"


def test_acting_stops_at_the_step_cap() -> None:
    budget, _ = make_budget(600.0)
    for _ in range(budget.max_steps):
        assert budget.may_act()
        budget.start_step()
    assert not budget.may_act()
    assert budget.steps_used == limits.MAX_STEPS


def test_recovery_is_bounded_by_code() -> None:
    budget, _ = make_budget(600.0)
    for _ in range(budget.max_recovery_passes):
        assert budget.may_recover()
        budget.start_recovery()
    assert not budget.may_recover()


def test_recovery_may_not_start_inside_the_verifier_reserve() -> None:
    """A recovery pass edits things, so it is acting and obeys the acting rule.

    Recovery that begins once only the reserve is left spends the very time the
    final verification needs, and a run that cannot verify cannot finish. The guard
    must therefore be no weaker than `may_act`.
    """
    budget, clock = make_budget(600.0)
    clock.advance(600.0 - budget.verify_reserve_s + 1.0)
    assert budget.remaining_s() > 0, "the run itself has not run out yet"
    assert not budget.may_act()
    assert not budget.may_recover()


def test_tool_timeout_shrinks_with_the_remaining_time() -> None:
    budget, clock = make_budget(600.0)
    assert budget.tool_timeout_s() == limits.TOOL_TIMEOUT_MAX_S

    clock.advance(600.0 - budget.verify_reserve_s - 5.0)
    assert budget.tool_timeout_s() == 5.0

    clock.advance(100.0)
    assert budget.tool_timeout_s() == 0.0, "never negative, never eats the reserve"


def test_usage_accumulates() -> None:
    budget, _ = make_budget()
    budget.add_usage(LlmReply(text="a", input_tokens=100, output_tokens=20))
    budget.add_usage(LlmReply(text="b", input_tokens=50, output_tokens=10))
    assert budget.total_tokens == 180
    assert budget.usage_reported


def test_one_reply_without_usage_marks_the_whole_total_unreported() -> None:
    budget, _ = make_budget()
    budget.add_usage(LlmReply(text="a", input_tokens=100, output_tokens=20))
    budget.add_usage(LlmReply(text="b", usage_reported=False))
    assert not budget.usage_reported, "totals are a lower bound and must say so"
    assert budget.total_tokens == 120


def test_from_env_reads_our_own_variable() -> None:
    clock = FakeClock()
    budget = Budget.from_env({"AGENT_TIMEOUT_S": "42"}, clock=clock)
    assert budget.total_s == 42.0


def test_from_env_ignores_garbage_and_non_positive_values() -> None:
    clock = FakeClock()
    for raw in ("", "   ", "abc", "0", "-5"):
        budget = Budget.from_env({"AGENT_TIMEOUT_S": raw}, clock=clock)
        assert budget.total_s == limits.DEFAULT_TASK_TIMEOUT_S, raw
        assert budget.may_act(), "a broken value must not disable the guards"


# --- the early deliverable checkpoint ---------------------------------------


def test_the_checkpoint_comes_long_before_the_outer_bound() -> None:
    budget, clock = make_budget(600.0)
    assert not budget.past_early_deadline()

    clock.advance(limits.EARLY_DELIVERABLE_DEADLINE_S - 1)
    assert not budget.past_early_deadline()

    clock.advance(2)
    assert budget.past_early_deadline()
    assert budget.may_act(), "producing an answer is work, not the end of the run"


def test_the_checkpoint_precedes_the_smallest_observed_task_limit() -> None:
    # The public set contains 120-second tasks and the runner never says which kind
    # this is. An answer has to exist before that floor, with room for startup.
    assert limits.EARLY_DELIVERABLE_DEADLINE_S < 120.0


def test_a_budget_smaller_than_the_checkpoint_is_all_early_phase() -> None:
    budget, clock = make_budget(60.0)
    assert budget.early_deadline_s <= budget.total_s - budget.verify_reserve_s

    clock.advance(budget.total_s - budget.verify_reserve_s)
    assert budget.past_early_deadline(), "a short run must not spend everything exploring"


# --- the per-call ceiling ----------------------------------------------------


def test_llm_timeout_is_bounded_by_the_early_deadline_from_the_very_first_call() -> None:
    # LLM_TIMEOUT_S (120) is wider than EARLY_DELIVERABLE_DEADLINE_S (90). A single
    # call left free to run the full per-call ceiling could, on its own, carry the
    # run past the point a deliverable was due -- before a runner with an
    # undeclared short timeout would have killed it, deliverable-less.
    budget, _ = make_budget(600.0)
    assert budget.llm_timeout_s() == budget.early_deadline_s


def test_llm_timeout_shrinks_as_the_early_deadline_approaches() -> None:
    budget, clock = make_budget(600.0)
    clock.advance(budget.early_deadline_s - 10.0)
    assert budget.llm_timeout_s() == 10.0


def test_llm_timeout_reverts_to_the_full_ceiling_once_past_the_early_deadline() -> None:
    budget, clock = make_budget(600.0)
    clock.advance(budget.early_deadline_s + 1.0)
    assert budget.llm_timeout_s() == limits.LLM_TIMEOUT_S


def test_allow_long_llm_lifts_the_pre_checkpoint_clamp() -> None:
    # F4: on a thinking endpoint the flag removes the 90s pre-checkpoint clamp, so a call
    # at step 1 gets the full ceiling instead of ~90s and its reasoning is not truncated.
    budget, _ = make_budget(600.0)
    assert budget.llm_timeout_s() == budget.early_deadline_s, "clamped by default"

    budget.allow_long_llm = True
    assert budget.llm_timeout_s() == limits.LLM_THINKING_TIMEOUT_S, "full ceiling once lifted"


def test_allow_long_llm_raises_the_ceiling_above_one_thinking_reply() -> None:
    """`F4-30b-paired`: lifting the clamp alone only moved the loss.

    The ceiling stayed `min(LLM_TIMEOUT_S, spendable)` = 120 s, and one raised thinking
    reply measured 6549 completion tokens = 110 s of generation plus a queue wait. The
    call timed out instead of being truncated, and two such timeouts demoted the run to
    `llm-unusable` -- two cells the control arm did not have.
    """
    budget, _ = make_budget(600.0)
    budget.allow_long_llm = True

    assert budget.llm_timeout_s() > limits.LLM_TIMEOUT_S
    assert budget.llm_timeout_s() >= 2 * 110.0


def test_a_fast_endpoint_never_reaches_the_thinking_ceiling() -> None:
    # The 7B regression guard at the budget level: with the flag unset nothing about the
    # per-call ceiling moves, before or after the checkpoint.
    budget, clock = make_budget(600.0)
    assert budget.llm_timeout_s() == budget.early_deadline_s

    clock.advance(budget.early_deadline_s + 1.0)
    assert budget.llm_timeout_s() == limits.LLM_TIMEOUT_S


def test_the_thinking_ceiling_still_stops_at_the_verifier_reserve() -> None:
    # The longer ceiling is a ceiling, not a grant: the reserve is what `spendable_s`
    # already excludes, so a lifted clamp can never spend the verifier's seconds.
    budget, clock = make_budget(600.0)
    budget.allow_long_llm = True
    clock.advance(600.0 - budget.verify_reserve_s - 7.0)

    assert budget.llm_timeout_s() == 7.0
    assert budget.remaining_s() > budget.llm_timeout_s()


def test_allow_long_llm_still_respects_the_spendable_budget() -> None:
    # The flag lifts only the checkpoint clamp; the verifier reserve and the deadline
    # still bound the call, so it never runs into the reserve.
    budget, clock = make_budget(120.0)
    budget.allow_long_llm = True
    clock.advance(60.0)

    assert budget.llm_timeout_s() == budget.spendable_s()
    assert budget.llm_timeout_s() < limits.LLM_TIMEOUT_S


def test_llm_timeout_never_exceeds_the_spendable_time() -> None:
    budget, clock = make_budget(600.0)
    clock.advance(600.0 - budget.verify_reserve_s - 5.0)
    assert budget.past_early_deadline()
    assert budget.llm_timeout_s() == 5.0


def test_llm_timeout_is_never_negative() -> None:
    budget, clock = make_budget(600.0)
    clock.advance(600.0)
    assert budget.llm_timeout_s() == 0.0


def test_llm_timeout_on_a_short_task_still_respects_the_checkpoint() -> None:
    budget, _ = make_budget(60.0)
    assert budget.llm_timeout_s() == budget.early_deadline_s


# --- the one-time step extension ---------------------------------------------
#
# Measured on the Qwen3.6-35B-A3B stand: a call costs 2-4 s, so the step cap arrives in
# 90-130 s of a 600 s budget and the run stops with 400+ s unused. The budget owns the
# clock half of that decision -- whether there is time worth spending -- and nothing
# else; whether the run is moving is read from the action history by the loop.


def at_the_cap(total_s: float = 600.0) -> tuple[Budget, FakeClock]:
    """A budget standing exactly at its step cap, which is where the question arises."""
    budget, clock = make_budget(total_s)
    for _ in range(budget.max_steps):
        budget.start_step()
    return budget, clock


def test_the_step_extension_needs_the_cap_to_be_reached_first() -> None:
    budget, _ = make_budget(600.0)
    assert not budget.may_extend_steps(), "there are still steps left to spend"
    assert budget.extend_steps() == 0
    assert budget.max_steps == limits.MAX_STEPS


def test_the_step_extension_is_granted_at_the_spare_threshold() -> None:
    budget, clock = at_the_cap()
    spare = 600.0 * limits.STEP_EXTENSION_MIN_SPARE_FRACTION
    clock.advance(600.0 - budget.verify_reserve_s - spare)

    assert budget.spendable_s() == spare
    assert budget.may_extend_steps()
    assert budget.extend_steps() == limits.STEP_EXTENSION
    assert budget.max_steps == limits.MAX_STEPS + limits.STEP_EXTENSION


def test_one_second_below_the_threshold_changes_nothing() -> None:
    # The slow-endpoint arm: the cap and the clock arrive together and the run ends as
    # it always did. A fast-path fix that also fires on the slow path is not one.
    budget, clock = at_the_cap()
    spare = 600.0 * limits.STEP_EXTENSION_MIN_SPARE_FRACTION
    clock.advance(600.0 - budget.verify_reserve_s - spare + 1.0)

    assert not budget.may_extend_steps()
    assert budget.extend_steps() == 0
    assert budget.max_steps == limits.MAX_STEPS
    assert not budget.may_act()


def test_the_step_checkpoint_fraction_ignores_the_extension() -> None:
    # `past_early_deadline` keys off the cap before the extension: producing starts at
    # 0.4 of the original thirty, not of the raised forty-five.
    budget, _ = at_the_cap()
    budget.extend_steps()

    assert budget.base_max_steps == limits.MAX_STEPS
    budget.steps_used = int(limits.MAX_STEPS * limits.EARLY_DELIVERABLE_STEP_FRACTION)
    assert budget.past_early_deadline()
