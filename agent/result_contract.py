"""Small contracts that can be derived from the original task instruction."""

from __future__ import annotations

import re
from pathlib import Path

_TARGET = re.compile(
    r"\b(?:create|write|make|save)\s+(?:an?\s+|the\s+)?file\s+(?:at|to)\s+"
    r"(?:(?P<quote>[`\"'])(?P<quoted>[^`\"']+)(?P=quote)|"
    r"(?P<bare>/[^\s,;\)\]}]+))",
    re.IGNORECASE,
)
_CONTENT = re.compile(
    r"\b(?:whose\s+(?:entire\s+)?content|(?:the\s+)?content)\s+"
    r"(?:(?:is|must\s+be|should\s+be)\s+)?exactly\s+"
    r"(?:the\s+single\s+word\s+)?"
    r"(?:(?P<quote>[`\"'])(?P<quoted>[^`\"']+)(?P=quote))",
    re.IGNORECASE,
)
_BRIDGE = re.compile(r"\s*(?:,\s*)?(?:with(?:\s+(?:its?|the))?\s+)?\s*\Z", re.IGNORECASE)
_TAIL = re.compile(
    r"\s*(?:\((?P<note>[^().!?;\n]*)\))?\s*\.?\s*\Z",
    re.IGNORECASE,
)
_NOTE = re.compile(
    r"\s*(?:"
    r"(?:(?:no|without)\s+(?:trailing\s+)?(?:newline|line\s+break|whitespace)\s+"
    r"(?:is\s+)?(?:required|needed|allowed)(?:\s*,?\s*but\s*)?)?"
    r"(?:the\s+file\s+(?:must|should)\s+contain\s+only\s+"
    r"(?:those|these|the)\s+[a-z0-9_-]+\s+(?:characters?|bytes?))?"
    r")\s*\Z",
    re.IGNORECASE,
)
_HEADING = re.compile(r"\A[ \t]*#{1,6}[ \t][^\n]*(?:\n|\Z)\s*")
"""One leading markdown heading: the title of the instruction, not a step of it.

`#` must open a real heading -- one to six of them followed by a space -- so `#1
priority ...` or a shell comment is still prose standing in front of the write. Only
the first is dropped; a second heading means the text has sections, and sections mean
more than one sentence.
"""

_NEGATION = re.compile(
    r"\b(?:do|does|did|must|should|shall|may|can)\s+not\b"
    r"|\b(?:never|don't|doesn't|didn't|mustn't|shouldn't|can't|cannot|not)\b",
    re.IGNORECASE,
)

MAX_EXACT_BYTES = 4096
"""Keep the content oracle for small text artifacts only."""


def _is_negated(instruction: str, start: int) -> bool:
    return bool(_NEGATION.search(instruction[:start]))


def _path(match: re.Match[str]) -> str:
    return match.group("quoted") or match.group("bare") or ""


def _value(match: re.Match[str]) -> str:
    return match.group("quoted") or ""


def exact_file_requirement(instruction: str) -> tuple[Path, bytes] | None:
    """Return one unambiguous exact-file requirement, or no contract.

    The parser is deliberately conservative. It only understands a positive file
    creation phrase followed by one quoted exact-content phrase in the same clause.
    Anything less specific stays on the normal model-chosen verifier path rather than
    turning a report or a repair instruction into an invented content oracle.
    """
    # A heading is dropped before anything is scanned, so every offset below belongs
    # to the same string. 4 of the 6 public instructions and all 18 extra ones open
    # with one, and the prefix guard read the title as an action taken before the
    # write -- which is the whole one-call path, on the class whose public tasks are
    # given 120 s. Nothing else relaxes: the guards on the prefix, the bridge and the
    # tail all still run, on the text behind the title.
    instruction = _HEADING.sub("", instruction, count=1)
    targets = [match for match in _TARGET.finditer(instruction) if not _is_negated(instruction, match.start())]
    contents = list(_CONTENT.finditer(instruction))
    if len(targets) != 1 or len(contents) != 1:
        return None

    target, content = targets[0], contents[0]
    prefix = instruction[: target.start()].strip()
    if prefix.lower() not in {"", "please"}:
        return None
    if target.end() > content.start():
        return None
    bridge = instruction[target.end() : content.start()]
    # The exact fast path is a one-step contract. A comma is allowed before the
    # content clause, but words or clause separators there can hide "and run ..."
    # or another imperative between the file write and its claimed bytes.
    if _BRIDGE.fullmatch(bridge) is None:
        return None

    tail = instruction[content.end() :]
    tail_match = _TAIL.fullmatch(tail)
    if tail_match is None:
        return None
    note = tail_match.group("note") or ""
    if note and _NOTE.fullmatch(note) is None:
        return None

    raw_path = _path(target)
    value = _value(content)
    path = Path(raw_path)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if not path.is_absolute() or not value or len(encoded) > MAX_EXACT_BYTES:
        return None
    return path, encoded
