"""Stop conditions the model does not get a vote in.

Every one of these exists because a small model fails silently: it repeats a broken
command, oscillates between two harmless calls, or narrates progress it is not making.
None of those raise, none of them look like an error, and all of them end the run at
zero when the deadline arrives. So the loop watches the shape of its own history
rather than asking the model whether it is stuck.

Two different answers come out of this module, and collapsing them would be wrong:

* `blocked` refuses **one action** before it runs and hands the reason back to the
  model. A repeated broken command is usually a misread error message, and the
  cheapest correction is saying so while there is still budget.
* `stuck` reports that the **history** has the shape of a cycle. What that costs
  depends on whether a deliverable exists yet, so the loop and not this module decides
  between ending the run and handing the model the observation.

The thresholds live in `agent.limits`. Two of them are OpenHands defaults read from
their source; the other two are starting values and are documented as such. Nothing
here has been calibrated against our own runs yet.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent import limits

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent.state import ActionRecord, AgentState

DIGEST_CHARS = 16


@dataclass(frozen=True)
class Signal:
    """A detector firing: what it was, and whether it stops the action or the loop."""

    kind: str
    """`block` for one refused action, `stuck` for the whole loop."""

    name: str
    detail: str
    """One line, safe to put in a prompt: it is shown to the model."""


def digest(text: str) -> str:
    """Short stable hash of an observation.

    Callers hash redacted text. This function cannot enforce that, so the rule lives
    with the caller in `AgentState.observe`, which is the only place that hashes tool
    output.
    """
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:DIGEST_CHARS]


def args_digest(args: Mapping[str, object]) -> str:
    """Hash of an argument set, insensitive to key order and to formatting.

    `{"path": p, "line": 1}` and `{"line": 1, "path": p}` are the same action. Sorting
    the keys before hashing is what makes the repeat detectors see them as one, and
    `default=str` keeps a `Path` from turning a detector into a crash.
    """
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return digest(canonical)


def _current_actions(state: AgentState) -> list[ActionRecord]:
    """Return detector evidence from the current environment epoch only."""
    return [record for record in state.actions if record.state_epoch == state.state_epoch]


def _identical_calls(state: AgentState, tool: str, args: str) -> list[ActionRecord]:
    """Earlier copies of this call that still describe the present workspace.

    A read is evidence only inside its epoch: a mutation may have changed what it
    would return. A mutating call is different -- it is what advances the epoch, so
    filtering it by epoch left every write the sole record of its window and twenty
    identical writes were never refused (F3, E-BASE-r1). Its copies count back until
    a *different* mutating action, which is what makes a rerun a new question.
    """
    same = [record for record in state.actions if record.tool == tool and record.args_digest == args]
    if not same or not same[-1].mutating:
        return [record for record in same if record.state_epoch == state.state_epoch]
    chain: list[ActionRecord] = []
    for record in reversed(state.actions):
        if record.tool == tool and record.args_digest == args:
            chain.append(record)
        elif record.mutating:
            break
    chain.reverse()
    return chain


def blocked(state: AgentState, tool: str, args: str) -> Signal | None:
    """Whether this exact call has already earned a refusal before it runs.

    Two shapes qualify, and they are refused for opposite reasons. A call that keeps
    **failing** is usually a misread error message. A call that keeps **succeeding with
    the same output** is an attractor: the observation it adds is byte-identical to one
    already in history, so a deterministic model reads the same context and emits the
    same action again. E-P2d spent 11 of 15 steps that way.

    A repeat whose output differs is still allowed. Re-reading a file that changed is
    work, and refusing it would be the detector inventing a rule nobody asked for.
    """
    identical = [record for record in _identical_calls(state, tool, args) if not record.ok]
    if len(identical) < limits.REPEAT_FAILURE_BLOCK:
        return _repeated_result(state, tool, args)

    last = identical[-1].error_code or "no exit code"
    return Signal(
        kind="block",
        name="repeat-failure",
        detail=(
            f"{tool} with these exact arguments already failed "
            f"{len(identical)} times ({last}). Change the arguments or the approach; "
            f"repeating it will not produce a different result."
        ),
    )


def _repeated_result(state: AgentState, tool: str, args: str) -> Signal | None:
    """Whether this exact call has already returned one and the same output."""
    identical = [record for record in _identical_calls(state, tool, args) if record.ok]
    if len(identical) < limits.REPEAT_RESULT_BLOCK:
        return None
    if len({record.result_fingerprint for record in identical}) != 1:
        return None

    return Signal(
        kind="block",
        name="repeat-result",
        detail=(
            f"{tool} with these exact arguments already ran {len(identical)} times and "
            f"returned the same output every time; that output is above in this "
            f"history. Reading it again adds nothing. Take a different action."
        ),
    )


def stuck(state: AgentState) -> Signal | None:
    """Whether the recent history has the shape of a loop rather than of work."""
    history = _current_actions(state)
    window = history[-limits.NO_PROGRESS_STOP :]
    if len(window) >= limits.NO_PROGRESS_STOP and not any(record.novel for record in window):
        # Checked first because it is the cheapest question and the broadest answer:
        # if anything new came back recently, the run is working, whatever shape the
        # calls have.
        return Signal(
            kind="stuck",
            name="no-progress",
            detail=(
                f"the last {limits.NO_PROGRESS_STOP} actions returned observations "
                f"this run had already seen"
            ),
        )

    if len(history) >= limits.EXACT_REPEAT_STOP:
        window = history[-limits.EXACT_REPEAT_STOP :]
        first = window[0]
        if all(
            record.tool == first.tool
            and record.args_digest == first.args_digest
            and record.result_fingerprint == first.result_fingerprint
            for record in window
        ):
            return Signal(
                kind="stuck",
                name="exact-repeat",
                detail=(
                    f"{first.tool} repeated {limits.EXACT_REPEAT_STOP} times with the "
                    f"same arguments and the same result"
                ),
            )

    if len(history) >= limits.ALTERNATING_STOP:
        window = history[-limits.ALTERNATING_STOP :]
        even = {(record.tool, record.args_digest) for record in window[0::2]}
        odd = {(record.tool, record.args_digest) for record in window[1::2]}
        if len(even) == 1 and len(odd) == 1 and even != odd:
            return Signal(
                kind="stuck",
                name="alternating",
                detail=(
                    f"two actions alternated for {limits.ALTERNATING_STOP} steps "
                    f"without the run moving on"
                ),
            )

    return None
