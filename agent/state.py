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

import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agent import limits, result_progress
from agent.detectors import args_digest, digest
from agent.ports import LlmError, LlmReply
from agent.redact import redact


@dataclass(frozen=True)
class Event:
    """One thing that happened, in the order it happened."""

    step: int
    kind: str
    """`phase`, `llm`, `tool`, `verify`, `budget`, `note` or `degrade`."""

    name: str
    detail: str
    duration_s: float = 0.0
    exit_code: int | None = None


@dataclass(frozen=True)
class ActionRecord:
    """One executed action, reduced to what the stuck detectors need.

    Deliberately not the action itself. Arguments and output are hashed, so the
    history stays a fixed size whatever the model sends and whatever a tool prints,
    and so a secret that appeared in an observation cannot survive here as plain text.
    """

    step: int
    tool: str
    args_digest: str
    result_fingerprint: str
    ok: bool
    state_epoch: int = 0
    """State version in which this action was observed."""

    error_code: str = ""
    """Short machine-readable failure label, e.g. `exit:127`. Empty on success."""

    novel: bool = True
    """Whether this action produced an observation the run had not already seen."""


TRACED_KINDS = frozenset({"thinking", "budget", "note"})
"""Audit kinds `AGENT_TRACE_BUDGET` mirrors to stdout.

The ones that explain a run's wall clock and its plan, and that nobody can see from
outside: the endpoint being recognised as a reasoning model (`thinking`, relayed from
`agent.llm`), the loop acting on it or on a detector (`budget`), and what the model
asked to remember between calls (`note`). Tool calls have their own switch.
"""

TRACE_DETAIL_CHARS = 200
"""How much of a traced note is printed. A line, not a dump."""


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

    allow_long_llm: bool = field(default=False)
    """Set once, for the rest of the run, when a thinking endpoint is observed: a reply
    carrying a reasoning field, a length cutoff with evidence of thinking behind it, or a
    call that timed out with no payload. It lifts the pre-checkpoint clamp on
    `llm_timeout_s` and raises its ceiling from `LLM_TIMEOUT_S` to
    `LLM_THINKING_TIMEOUT_S`: on such an endpoint a single thinking call needs more than
    `early_deadline_s - elapsed`, and clamping it truncates the reasoning and loses the
    call (F4, `F4-30b-paired`).

    One-way; the verifier reserve and the task deadline still bound the call. A bare
    length cutoff with an ordinary non-empty answer does *not* set it -- that is a model
    looping, not a model thinking, and treating the two alike cost 250 s of wall clock on
    the fast 7B for the same zero (`F4-7b-regression-4080`)."""

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

        Steps count as well as seconds, and for the same reason the clock does: on an
        endpoint fast enough to spend the whole step cap inside the time checkpoint,
        a purely temporal one never arrives.
        """
        if self.elapsed_s() >= self.early_deadline_s:
            return True
        return self.steps_used >= self.max_steps * limits.EARLY_DELIVERABLE_STEP_FRACTION

    def may_recover(self) -> bool:
        """Whether a directed retry after a red verifier is still allowed.

        Recovery edits the workspace, so it is acting and obeys the acting rule. The
        earlier `remaining_s() > 0` was weaker than `may_act`, which let a retry start
        once only the reserve was left -- spending the very time the re-verification
        needs. A run that cannot verify cannot finish.
        """
        return self.recovery_passes_used < self.max_recovery_passes and self.spendable_s() > 0

    def tool_timeout_s(self) -> float:
        """Timeout for one shell command: never long enough to eat the reserve."""
        return max(0.0, min(limits.TOOL_TIMEOUT_MAX_S, self.spendable_s()))

    def llm_timeout_s(self) -> float:
        """Ceiling for one logical LLM call: never longer than the per-endpoint ceiling or
        the spendable time, and -- before the early checkpoint, on a fast endpoint --
        never long enough on its own to run past it. `LLM_TIMEOUT_S` (120s) is wider than
        `early_deadline_s` (90s by default); left uncapped on a fast model, a single call
        could carry the run past the point a deliverable was due before a runner with an
        undeclared short timeout would have killed it.

        Two things change once `allow_long_llm` is set, i.e. once a thinking endpoint is
        observed. The pre-checkpoint clamp is lifted, because there a single call needs
        more than the clamp allows and clamping it truncates the reasoning and loses the
        call. And the ceiling itself becomes `LLM_THINKING_TIMEOUT_S`, because 120 s is
        shorter than one measured thinking reply on the evaluation-class stand, so the
        lifted clamp would only have moved the loss from a truncation to a timeout
        (`F4-30b-paired`: two runs demoted to `llm-unusable` for exactly that).

        The verifier reserve and the task deadline bound the call either way: both
        branches take the minimum with `spendable_s()`, which is what the reserve is
        subtracted from.
        """
        ceiling = limits.LLM_THINKING_TIMEOUT_S if self.allow_long_llm else limits.LLM_TIMEOUT_S
        ceiling = min(ceiling, self.spendable_s())
        if not self.allow_long_llm and not self.past_early_deadline():
            ceiling = min(ceiling, self.early_deadline_s - self.elapsed_s())
        return max(0.0, ceiling)

    def start_step(self) -> int:
        """Consume one step and return its number, starting at 1."""
        self.steps_used += 1
        return self.steps_used

    def start_recovery(self) -> int:
        self.recovery_passes_used += 1
        return self.recovery_passes_used

    def add_usage(self, reply: LlmReply | LlmError) -> None:
        # Errors carry usage too: a demoted or truncated attempt was still billed, and
        # the tie-break counts it. Both types expose the same three fields.
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

    baseline: dict[str, str] = field(default_factory=dict)
    """What the paths the task named contained before this run started.

    So that `finish` cannot hand back an untouched input file as the answer. Content
    and not timestamps: archives restore mtimes and `touch` rewrites them, and both
    mistakes are one-directional -- a real deliverable called stale, or an untouched
    file called solved.
    """

    source_snapshot: result_progress.SourceSnapshot | None = None
    """Bounded production source/config snapshot, separate from named-path baseline.

    ``None`` is used by low-level tests and adapters that do not own preflight. The
    production entrypoint always fills it before the first agent action.
    """

    named_paths: tuple[Path, ...] = ()
    """Absolute paths the task text names, in the order it names them.

    The write policy, not a suggestion: a path the task itself names is part of the
    task's contract, and refusing to write there turns a correct action into a wrong
    artifact (measured in E011). Nothing the *environment* says reaches this tuple --
    file contents, logs and command output are not the task text.
    """

    hypothesis: str = ""
    """Current guess about what the task is and how it will be checked."""

    artifacts: list[Path] = field(default_factory=list)
    """Files this run created or modified, for the final report and for cleanup."""

    read_paths: set[Path] = field(default_factory=set, repr=False)
    """Resolved paths this run has actually read (read_range, or a read-only shell that
    opened the file). A code-fix write_file to an unread existing source is refused, so
    the run cannot overwrite a module it never opened -- the 14B fix-sqli failure."""

    audit: list[Event] = field(default_factory=list)
    """Append-only trace. Nothing removes entries; truncation happens at render time."""

    actions: list[ActionRecord] = field(default_factory=list)
    """Sliding window of the last `limits.ACTION_HISTORY` actions, for the detectors.

    Separate from `audit` on purpose: the trace is append-only and exists to be read
    by a human afterwards, while this window is a fixed-size working set the loop
    queries on every step. Merging them would make one of the two wrong.
    """

    state_epoch: int = 0
    """Incremented after a file or service mutation invalidates old observations."""

    seen_results: set[str] = field(default_factory=set, repr=False)
    """Every result fingerprint this run has produced, for the progress detector."""

    _artifacts_seen: int = field(default=0, repr=False)
    """How many artifacts existed at the previous observation, so that producing one
    counts as progress even when the command that produced it printed nothing new."""

    secrets: tuple[str, ...] = field(default=(), repr=False)
    """Values that must not reach an observation, a hash or the trace."""

    notes: list[tuple[int, str]] = field(default_factory=list)
    """The model's own scratchpad: `(step, text)`, oldest first, bounded by `remember`.

    The one thing in this object the model writes and reads back. Everything else it
    sends is consumed by the step it arrived in; a reasoning endpoint drops its own
    reasoning between calls and the prompt keeps only the last observations, so without
    this list a plan made at step 4 is gone by step 9 -- measured as thirty steps of
    re-reading the same eight files with no edit ever proposed (JOURNAL, 16.09 05:50).

    Untrusted text, exactly like an observation: it is stored redacted and bounded, it
    is never parsed, matched or executed, and no branch of the loop reads it to decide
    what to run.
    """

    event_sink: Callable[[Event], None] | None = field(default=None, repr=False)
    """Optional live diagnostic sink. Exceptions are isolated; synchronous latency is not."""

    def advance_state_epoch(self) -> int:
        """Forget detector evidence from before a possible environment mutation."""
        self.state_epoch += 1
        self.seen_results.clear()
        return self.state_epoch

    def repeats_last_action(self, tool: str, args: Mapping[str, object], output: str) -> bool:
        """Whether this action is a copy of the previous one with the same observation.

        Same tool, same arguments, same redacted output, same epoch. Such a repeat
        changed nothing the previous copy had not already changed, so the caller keeps
        the epoch instead of opening a new one: on the 4080 7B stand nineteen identical
        `write_file` calls each opened a fresh epoch, and no detector ever saw more than
        one of them (F4-7b-regression-4080: 30 steps for nothing on an audit fixture).
        """
        # Every action of the current epoch, not just the previous one: a read between
        # two identical writes does not advance the epoch, and looking only at
        # `actions[-1]` let a write/list_dir alternation open a fresh epoch per write
        # (critic, 16.09). Actions before the epoch's first entry are already history.
        wanted_args = args_digest(args)
        wanted_result = digest(redact(output, self.secrets))
        for previous in reversed(self.actions):
            if previous.state_epoch != self.state_epoch:
                break
            if (
                previous.tool == tool
                and previous.args_digest == wanted_args
                and previous.result_fingerprint == wanted_result
            ):
                return True
        return False

    def remember(self, text: str) -> str:
        """Keep one note for the next steps. Returns what was stored, "" when nothing was.

        Every bound the scratchpad has lives here rather than at the call sites, because
        there are two of them -- the step's own action and the read-only batch -- and a
        bound enforced in one place is a bound.

        In order, and the order is the point:

        * **Redact first.** A secret is replaced while it is still whole; truncating
          first could cut one in half and leave the half the redactor no longer matches.
        * **One line.** Control characters and line breaks collapse to spaces, so a note
          cannot forge a second `[step N]` line, an observation, or a terminal escape in
          the trace. The model writes a note; it does not write the block.
        * **Then truncate**, to `NOTE_MAX_CHARS`, never refusing: the action this note
          rode in on is about to run either way.
        * **Then evict**, oldest first, until the list fits both `NOTES_MAX_ENTRIES` and
          `NOTES_MAX_CHARS`. The newest note always survives, whatever the budget.

        An empty or whitespace-only note is not a note, and neither is a repeat of the
        one already at the end: a model that writes the same sentence every step would
        otherwise spend the whole budget saying it once per step.
        """
        redacted = redact(str(text), self.secrets)
        plain = "".join(
            " " if character < " " or character == "\x7f" else character for character in redacted
        )
        cleaned = " ".join(plain.split())
        if not cleaned:
            return ""
        cleaned = cleaned[: limits.NOTE_MAX_CHARS]
        if self.notes and self.notes[-1][1] == cleaned:
            return ""

        self.notes.append((self.budget.steps_used, cleaned))
        while len(self.notes) > 1 and (
            len(self.notes) > limits.NOTES_MAX_ENTRIES
            or sum(len(entry) for _, entry in self.notes) > limits.NOTES_MAX_CHARS
        ):
            del self.notes[0]
        self.record("note", "kept", cleaned)
        return cleaned

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
        self._trace(event)
        if self.event_sink is not None:
            try:
                self.event_sink(event)
            except Exception as error:
                # Diagnostics are strictly best-effort. A closed runner pipe or a
                # formatting defect must never turn an otherwise valid task into 0.
                #
                # But a sink that dies silently is indistinguishable from a run that
                # produced no events, and that ambiguity cost a session: E017's logs
                # were read as a broken sink when they simply predated it. One line on
                # stderr, the exception's type and nothing else -- the event that
                # failed may carry trace content, and the message may quote it.
                self.event_sink = None
                try:
                    print(
                        f"agent phase: telemetry disabled after {type(error).__name__}",
                        file=sys.stderr,
                        flush=True,
                    )
                except Exception:
                    pass
        return event

    def _trace(self, event: Event) -> None:
        """Mirror a thinking/budget note to stdout when the operator asked for it.

        Same shape and same rules as the `AGENT_TRACE_TOOLS` line in `agent.loop`: off
        unless the variable is set, redacted before it is printed, bounded, and never
        allowed to raise -- a closed pipe must not turn a solvable task into a zero.
        Why it exists: the chain that decides whether a call gets 90 s or 240 s is
        entirely internal, so a run that spent its budget waiting looks identical from
        outside to one that spent it reasoning.
        """
        if event.kind not in TRACED_KINDS:
            return
        try:
            if not os.environ.get(limits.TRACE_BUDGET_ENV_NAME):
                return
            detail = redact(event.detail, self.secrets)[:TRACE_DETAIL_CHARS]
            print(
                f"agent {event.kind}: step={event.step} {event.name} {detail}".rstrip(),
                flush=True,
            )
        except Exception:
            pass

    def start_phase(self, name: str) -> float:
        """Emit a flushed start marker, then return the phase's timing origin."""
        self.record("phase", name, "start")
        return self.budget.clock()

    def finish_phase(self, name: str, started_at: float) -> Event:
        """Record one completed phase without allowing a backwards test clock."""
        duration = max(0.0, self.budget.clock() - started_at)
        return self.record("phase", name, "end", duration_s=duration)

    def observe(
        self,
        tool: str,
        args: Mapping[str, object],
        output: str,
        *,
        ok: bool = True,
        error_code: str = "",
    ) -> ActionRecord:
        """Record an executed action in the detector window.

        The output is redacted before it is hashed, not after. A fingerprint taken
        from raw text would be a stable identifier derived from a secret, which is the
        same leak in a shorter form.
        """
        fingerprint = digest(redact(output, self.secrets))
        produced = len(self.artifacts) > self._artifacts_seen
        self._artifacts_seen = len(self.artifacts)
        novel = produced or fingerprint not in self.seen_results
        self.seen_results.add(fingerprint)

        record = ActionRecord(
            step=self.budget.steps_used,
            tool=tool,
            args_digest=args_digest(args),
            result_fingerprint=fingerprint,
            ok=ok,
            state_epoch=self.state_epoch,
            error_code=error_code,
            novel=novel,
        )
        self.actions.append(record)
        del self.actions[: -limits.ACTION_HISTORY]
        return record
