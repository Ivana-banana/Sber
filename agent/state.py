"""What the run knows about itself: budget, trace and working set.

Two ideas are carried over from earlier projects.

From ai-v-massy (`src/pipeline/state.py`): the trace is an append-only list that
lives in the state itself. Observability is then a by-product of running the loop
rather than a second logging mechanism that drifts from what actually happened.

From AIOS (`aios/loop.py`): the object that carries the result is honest about what
has been proven. Here that shows up as `usage_reported` and as the fact that nothing
in this module can decide a task is solved -- only the verifier can.

The budget is enforced by code. The model never gets to extend a deadline, raise the
step cap or spend the verifier reserve; those are read-only facts to it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agent import limits
from agent.ports import LlmReply


@dataclass(frozen=True)
class Event:
    """One thing that happened, in the order it happened."""

    step: int
    kind: str
    """`llm`, `tool`, `verify`, `budget` or `degrade`."""

    name: str
    detail: str
    duration_s: float = 0.0
    exit_code: int | None = None


def _reserve_for(total_s: float) -> float:
    """How much of the budget is withheld for verification and a clean exit."""
    reserve = total_s * limits.VERIFY_RESERVE_FRACTION
    reserve = max(limits.VERIFY_RESERVE_MIN_S, min(limits.VERIFY_RESERVE_MAX_S, reserve))
    return min(reserve, total_s)


@dataclass
class Budget:
    """Time, steps and tokens, all of them finite and all of them owned by the loop."""

    total_s: float
    max_steps: int = limits.MAX_STEPS
    max_recovery_passes: int = limits.MAX_RECOVERY_PASSES

    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    started_at: float = field(default=0.0)

    steps_used: int = 0
    recovery_passes_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    usage_reported: bool = True
    """False as soon as one call comes back without usage: totals are then a lower
    bound, not a measurement, and must be reported as such."""

    verify_reserve_s: float = field(default=0.0)
    early_deadline_s: float = field(default=limits.EARLY_DELIVERABLE_DEADLINE_S)
    """When a checkable answer must exist, independent of the outer bound."""

    def __post_init__(self) -> None:
        if self.started_at == 0.0:
            self.started_at = self.clock()
        if self.verify_reserve_s == 0.0:
            self.verify_reserve_s = _reserve_for(self.total_s)
        # A budget shorter than the checkpoint has no room for a two-phase plan; the
        # whole run is then the early phase.
        self.early_deadline_s = min(self.early_deadline_s, self.total_s - self.verify_reserve_s)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> Budget:
        """Read the deadline from the environment, falling back to the placeholder.

        Only names we own are consulted (`limits.TIMEOUT_ENV_NAMES`). A malformed or
        non-positive value is treated as absent rather than trusted: a deadline of
        zero would disable every guard in this class.
        """
        total_s = limits.DEFAULT_TASK_TIMEOUT_S
        for name in limits.TIMEOUT_ENV_NAMES:
            raw = env.get(name, "").strip()
            if not raw:
                continue
            try:
                candidate = float(raw)
            except ValueError:
                continue
            if candidate > 0:
                total_s = candidate
                break
        return cls(total_s=total_s, clock=clock)

    def elapsed_s(self) -> float:
        return self.clock() - self.started_at

    def remaining_s(self) -> float:
        return self.total_s - self.elapsed_s()

    def spendable_s(self) -> float:
        """Time available for acting, i.e. everything outside the verifier reserve."""
        return self.remaining_s() - self.verify_reserve_s

    def may_act(self) -> bool:
        """Whether another act/observe iteration may start."""
        return self.steps_used < self.max_steps and self.spendable_s() > 0

    def past_early_deadline(self) -> bool:
        """Whether a best-effort answer should already exist on disk.

        The runner never tells us the real limit, and the public set contains both
        120-second and 600-second tasks. Rather than betting on one of them, the loop
        treats this moment as the point where exploring stops and producing starts:
        past it, being killed costs the refinement, never the deliverable.
        """
        return self.elapsed_s() >= self.early_deadline_s

    def may_recover(self) -> bool:
        """Whether a directed retry after a red verifier is still allowed."""
        return self.recovery_passes_used < self.max_recovery_passes and self.remaining_s() > 0

    def tool_timeout_s(self) -> float:
        """Timeout for one shell command: never long enough to eat the reserve."""
        return max(0.0, min(limits.TOOL_TIMEOUT_MAX_S, self.spendable_s()))

    def start_step(self) -> int:
        """Consume one step and return its number, starting at 1."""
        self.steps_used += 1
        return self.steps_used

    def start_recovery(self) -> int:
        self.recovery_passes_used += 1
        return self.recovery_passes_used

    def add_usage(self, reply: LlmReply) -> None:
        self.input_tokens += reply.input_tokens
        self.output_tokens += reply.output_tokens
        if not reply.usage_reported:
            self.usage_reported = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class AgentState:
    """Everything one run carries between steps."""

    instruction: str
    """Raw task text. Untrusted data, never an instruction to the agent itself."""

    workspace: Path
    budget: Budget

    hypothesis: str = ""
    """Current guess about what the task is and how it will be checked."""

    artifacts: list[Path] = field(default_factory=list)
    """Files this run created or modified, for the final report and for cleanup."""

    audit: list[Event] = field(default_factory=list)
    """Append-only trace. Nothing removes entries; truncation happens at render time."""

    def record(
        self,
        kind: str,
        name: str,
        detail: str = "",
        *,
        duration_s: float = 0.0,
        exit_code: int | None = None,
    ) -> Event:
        event = Event(
            step=self.budget.steps_used,
            kind=kind,
            name=name,
            detail=detail,
            duration_s=duration_s,
            exit_code=exit_code,
        )
        self.audit.append(event)
        return event
