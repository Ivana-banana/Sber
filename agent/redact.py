"""Keeping credentials out of the trace.

The realistic leak is not us printing the key on purpose. It is the agent running
something like `env` or `cat /proc/self/environ` while exploring the machine, and the
endpoint key landing in tool output, from there in the trace, and from there in a log
we attach to an experiment record.

So redaction works on values, not on shapes: whatever the environment says is a
secret gets replaced wherever it appears in text we are about to keep.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

PLACEHOLDER = "<redacted>"

SECRET_NAME_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
"""Environment variable names whose values are treated as credentials."""

MIN_SECRET_LENGTH = 8
"""Short values are not redacted.

A one-character secret would turn every occurrence of that character into a
placeholder and destroy the diagnostic value of the whole trace. Nothing usable as an
API key is this short.
"""


def secrets_from_env(env: Mapping[str, str]) -> tuple[str, ...]:
    """Collect credential values worth hiding, longest first.

    Longest first matters: if one secret is a substring of another, replacing the
    short one first would leave a mangled remainder of the long one in the text.
    """
    found = set()
    for name, value in env.items():
        if not any(marker in name.upper() for marker in SECRET_NAME_MARKERS):
            continue
        value = value.strip()
        if len(value) >= MIN_SECRET_LENGTH:
            found.add(value)
    return tuple(sorted(found, key=len, reverse=True))


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every occurrence of every secret in `text`."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, PLACEHOLDER)
    return text
