"""content_health: the coarse structural score best-snapshot defends.

The score exists to answer one question the verifier is too expensive to ask on
every write: did this overwrite empty a deliverable that once carried a real one. It
is deliberately not a correctness oracle -- a right and a wrong audit report both
score 2 -- because best-snapshot must never override one plausible answer with
another. Only the 0-vs-2 boundary drives a restore.
"""

from __future__ import annotations

import pytest

from agent.snapshot import content_health


@pytest.mark.parametrize("text", ["", "   ", "\n\t ", "\r\n"])
def test_empty_or_blank_is_zero(text: str) -> None:
    assert content_health(text) == 0


@pytest.mark.parametrize(
    "text",
    [
        '{"findings": [{"id": 1, "vuln": "sqli"}]}',  # audit report with findings
        '{"issues": [], "findings": ["x"]}',  # a non-empty list is enough
        "[1, 2, 3]",  # a bare non-empty array
        "user=alice\nip=10.0.0.1\naction=login",  # forensics key=value
        "k1=v1\nk2=v2",  # two key=value lines is the floor
    ],
)
def test_structured_and_substantive_is_two(text: str) -> None:
    assert content_health(text) == 2


@pytest.mark.parametrize(
    "text",
    [
        '{"findings": []}',  # the exact overwrite this rescues: valid JSON, no findings
        "{}",  # empty object
        "[]",  # empty array
        '"flag{abc}"',  # a JSON string scalar
        "flag{abc123}",  # a plain flag, one line, not key=value
        "just a sentence of prose without structure",
        "a=1",  # a single key=value line is not yet a report
        "line one\nline two\nline three",  # several lines, none key=value
    ],
)
def test_present_but_thin_is_one(text: str) -> None:
    assert content_health(text) == 1


def test_health_is_monotone_under_the_overwrite_it_defends() -> None:
    good = '{"findings": [{"id": 1}]}'
    empty = '{"findings": []}'
    assert content_health(good) > content_health(empty) > 0
