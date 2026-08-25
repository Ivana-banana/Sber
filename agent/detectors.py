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
* `stuck` ends the **loop** and sends it to finalisation. Once the history has the
  shape of a cycle, more steps buy tokens and nothing else.

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
    from agent.state import AgentState

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


def blocked(state: AgentState, tool: str, args: str) -> Signal | None:
    """Whether this exact call has already failed often enough to refuse it.

    Only failures count. A successful call that repeats is usually the model re-reading
    a file it needs, which is cheap and legitimate; refusing it would be the detector
    inventing a rule nobody asked for.
    """
    identical = [
        record
        for record in state.actions
        if not record.ok and record.tool == tool and record.args_digest == args
    ]
    if len(identical) < limits.REPEAT_FAILURE_BLOCK:
        return None

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


def stuck(state: AgentState) -> Signal | None:
    """Whether the recent history has the shape of a loop rather than of work."""
    history = state.actions
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
