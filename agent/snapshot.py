"""Best-snapshot: never let a run's deliverable end up empty when it once had a real one.

A small model that has written a structured deliverable can later truncate or blank
the file -- an audit report or a `key=value` incident report becoming a zero-byte or
whitespace-only file. The external grader reads whatever is on disk when the run ends,
so a deliverable that was populated and is now empty scores zero for nothing. The
observed shape is a report whose findings passed every gate, rewritten to an empty
findings list twenty times over.

`content_health` scores deliverable content on a coarse 0/1/2 structural scale. The
loop remembers the *structured* (level-2) content written to each path and, at
`_finalise` only, restores it **only when the file on disk has since become empty or
gone**. It runs only on runs that never reached a green finish and never claims the
task solved -- it only refills a deliverable the run emptied.

The narrow trigger is deliberate and load-bearing. The score is a structure signal,
not a correctness oracle: a wrong verbose report scores 2 and a *correct terse* one
(an audit with no findings, a forensics answer that is a single value) scores 1, so
restoring on any health drop would roll a correct answer back to a wrong one and turn
a pass into a zero. Refilling only an empty file avoids that entirely: an empty
deliverable is never the correct final state for the tasks that produce a structured
one, so putting the last structured content back can only help or be neutral. A
non-empty file is always left exactly as the model last wrote it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from agent.state import AgentState
from agent.verify import _resolve_deliverable

_KEY_VALUE = re.compile(r"^[A-Za-z_][\w.-]*=\S")

MAX_SNAPSHOT_BYTES = 256 * 1024
"""Ceiling on a remembered revision. A snapshot is held for the whole run, and a
deliverable larger than this is not the case this defends -- the reports it rescues
are kilobytes."""


def content_health(text: str) -> int:
    """Structural, kind-agnostic health of deliverable content.

    2 -- structured and substantive: JSON carrying a non-empty list (audit findings),
         or at least two `key=value` lines (a forensics report).
    1 -- present but thin: any other non-empty content, including valid JSON with no
         findings (`{"findings": []}`), a bare flag, prose, or a single value.
    0 -- empty or whitespace only.

    Only the 0-vs-2 boundary drives restore, so the 1/2 split need not be a quality
    judgement: level 1 is 'the model wrote something, leave it alone', level 0 is
    'the file is gone, and it once was not'.
    """
    stripped = text.strip()
    if not stripped:
        return 0
    if stripped[:1] in "{[":  # only structures can carry a non-empty list; skip scanning prose/dumps
        try:
            value = json.loads(stripped)
        except ValueError:
            value = None
        if isinstance(value, list) and value:
            return 2
        if isinstance(value, dict) and any(isinstance(item, list) and item for item in value.values()):
            return 2
        if value is not None:
            return 1
    lines = [line for line in stripped.splitlines() if line.strip()]
    key_value = sum(1 for line in lines if _KEY_VALUE.match(line))
    if len(lines) >= 2 and key_value >= max(2, (len(lines) + 1) // 2):
        return 2
    return 1


def remember(state: AgentState, named: Path, content: str) -> None:
    """Keep the latest *structured* (level-2) content written to `named`.

    Only level-2 content is stored, so a plain code patch, a flag, or a terse answer
    is never a snapshot that could be resurrected later, and a large unstructured
    scratch write costs only a `content_health` call, not a stored copy. Ties keep the
    most recent write. The bytes are taken from the string just handed to `write_file`,
    which is exactly what landed on disk, so no extra read is needed.
    """
    if content_health(content) == 2:
        state.best_content[named] = content.encode("utf-8")


def remember_from_disk(state: AgentState, named: Path) -> None:
    """Snapshot what a successful write actually left on disk.

    Read back rather than taken from the call's arguments, because `write_json`
    serialises its own bytes: a snapshot built from the request could disagree with the
    file the grader will read, and restoring it would put fiction back. The read is
    bounded and every failure is silent -- a snapshot is an optimisation, and a run
    must never end worse because one could not be taken.
    """
    resolved = _resolve_deliverable(named, state.workspace, state.named_paths)
    if resolved is None:
        return
    try:
        if resolved.stat().st_size > MAX_SNAPSHOT_BYTES:
            return
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    remember(state, named, content)


def restore(state: AgentState, named: Path) -> str | None:
    """Refill `named` from its last structured snapshot, but only if it is now empty.

    Returns a short note for the trace when it acted, `None` when it left disk alone.
    A non-empty file on disk is always kept as the model last wrote it -- restore
    never overrides one answer with another, only refills a deliverable the run blanked
    or deleted.

    The path is resolved through `agent.verify`'s own guard rather than joined here, so
    a snapshot can only ever be written back inside the run's write policy -- the same
    confinement `write_file` applied when the content was produced.
    """
    best = state.best_content.get(named)
    if best is None:
        return None
    resolved = _resolve_deliverable(named, state.workspace, state.named_paths)
    if resolved is None:
        return None
    try:
        current = resolved.read_bytes()
    except OSError:
        current = b""
    if content_health(current.decode("utf-8", "replace")) != 0:
        return None
    try:
        resolved.write_bytes(best)
    except OSError:
        return None
    return "refilled an emptied deliverable from its last structured revision"
