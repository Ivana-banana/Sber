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
    r"\b(?:whose\s+entire\s+content|(?:the\s+)?content)\s+"
    r"(?:is|must\s+be|should\s+be)\s+exactly\s+"
    r"(?:the\s+single\s+word\s+)?"
    r"(?:(?P<quote>[`\"'])(?P<quoted>[^`\"']+)(?P=quote))",
    re.IGNORECASE,
)

MAX_EXACT_BYTES = 4096
"""Keep the content oracle for small text artifacts only."""


def _is_negated(instruction: str, start: int) -> bool:
    prefix = instruction[max(0, start - 12) : start].lower()
    return bool(re.search(r"(?:\bdo\s+not|\bdon't|\bnever)\s+$", prefix))


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
    targets = [match for match in _TARGET.finditer(instruction) if not _is_negated(instruction, match.start())]
    contents = list(_CONTENT.finditer(instruction))
    if len(targets) != 1 or len(contents) != 1:
        return None

    target, content = targets[0], contents[0]
    if target.end() > content.start():
        return None
    bridge = instruction[target.end() : content.start()]
    if any(mark in bridge for mark in ".!?\n"):
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
