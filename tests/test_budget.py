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
