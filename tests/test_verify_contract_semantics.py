"""Negative fixtures generalised from `docs/research/verifier-contract.md` §5.

That document read all six public task verifiers and found eight ways a "correct"
answer still scores zero: right words in the wrong channel, right file at the wrong
path, right token buried in extra text, a structurally valid payload wrapped in
diagnostic noise, a value under the wrong-case key, one extra line, one stray space,
and a patch that never reaches the code path actually executed.

None of those eight are properties of `bye-file` or `incident-log-forensics`
specifically -- they are properties of how *any* strict comparison fails, and that is
what makes them worth keeping past the six public tasks. So every fixture here is named
and shaped generically: no public task name, path or reference answer appears below. If
a fixture could not be described without naming one of the six, it would be testing
overfit to the public set instead of the semantic class -- which is exactly the trap
`CLAUDE.md` and `PLAN-KT4.md` both call out for production code and tests alike.

`verify()` itself has no format-specific logic; it checks existence, non-emptiness, and
the exit code of whatever command it is given. So "testing §5 against verify()" mostly
means proving that mechanism carries each semantic class faithfully once a verify
command encodes it -- the eight patterns below are eight verify commands, not eight new
branches in `agent/verify.py`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.state import AgentState, Budget
from agent.verify import verify

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="target image is Linux; /bin/sh absent on Windows")


def make_state(tmp_path: Path) -> AgentState:
    return AgentState(
        instruction="produce a checkable result",
        workspace=tmp_path,
        budget=Budget(total_s=600.0, clock=lambda: 0.0),
    )


# --- 1. right words, wrong channel -------------------------------------------------


def test_the_right_text_somewhere_that_is_not_the_deliverable_does_not_count(tmp_path: Path) -> None:
    # Stand-in for "printed the correct answer to stdout, never wrote the file":
    # the correct value exists in the workspace, just not at the path being checked.
    (tmp_path / "scratch-notes.txt").write_text("EXPECTED", encoding="utf-8")
    state = make_state(tmp_path)

    result = verify(state, deliverable=tmp_path / "answer.txt", command="")

    assert result.ok is False
    assert result.stage == "existence"


# --- 2. right content, wrong path ---------------------------------------------------


def test_the_right_content_at_a_decoy_path_does_not_satisfy_the_real_one(tmp_path: Path) -> None:
    (tmp_path / "decoy" / "answer.txt").parent.mkdir()
    (tmp_path / "decoy" / "answer.txt").write_text("EXPECTED", encoding="utf-8")
    state = make_state(tmp_path)

    result = verify(state, deliverable=tmp_path / "answer.txt", command="")

    assert result.ok is False
    assert result.stage == "existence"


# --- 3. right token, extra surrounding text ------------------------------------------


@POSIX_ONLY
def test_an_exact_equality_check_rejects_a_correct_token_with_extra_text(tmp_path: Path) -> None:
    target = tmp_path / "answer.txt"
    state = make_state(tmp_path)
    check = '[ "$(cat answer.txt)" = "EXPECTED" ]'

    target.write_text("EXPECTED", encoding="utf-8")
    assert verify(state, deliverable=target, command=check).ok is True

    target.write_text("note: done\nEXPECTED", encoding="utf-8")
    result = verify(state, deliverable=target, command=check)
    assert result.ok is False
    assert result.stage == "command"


# --- 4. structurally valid payload wrapped in diagnostic noise ----------------------


@POSIX_ONLY
def test_a_structured_payload_prefixed_with_diagnostic_text_fails_to_parse(tmp_path: Path) -> None:
    target = tmp_path / "result.json"
    state = make_state(tmp_path)
    check = f'python3 -c "import json; json.load(open({target.name!r}))"'

    target.write_text('{"status": "ok"}', encoding="utf-8")
    assert verify(state, deliverable=target, command=check).ok is True

    target.write_text('note: analysis complete\n{"status": "ok"}', encoding="utf-8")
    result = verify(state, deliverable=target, command=check)
    assert result.ok is False
    assert result.stage == "command"


# --- 5. right value, wrong-case key --------------------------------------------------


@POSIX_ONLY
def test_a_case_sensitive_key_check_rejects_a_key_that_only_differs_in_case(tmp_path: Path) -> None:
    target = tmp_path / "result.json"
    state = make_state(tmp_path)
    # grep -F, not -Fi: the real find-sqli-login verifier requires the exact lowercase
    # key `findings` and fails a same-word different-case key the same way.
    check = f'grep -qF \'"status"\' {target.name}'

    target.write_text('{"status": "ok"}', encoding="utf-8")
    assert verify(state, deliverable=target, command=check).ok is True

    target.write_text('{"Status": "ok"}', encoding="utf-8")
    result = verify(state, deliverable=target, command=check)
    assert result.ok is False
    assert result.stage == "command"


# --- 6. exactly N lines, no more --------------------------------------------------


@POSIX_ONLY
def test_an_exact_line_count_check_rejects_one_extra_line(tmp_path: Path) -> None:
    target = tmp_path / "report.txt"
    state = make_state(tmp_path)
    check = f'[ "$(wc -l < {target.name})" -eq 2 ]'

    target.write_text("field_one=1\nfield_two=2\n", encoding="utf-8")
    assert verify(state, deliverable=target, command=check).ok is True

    target.write_text("field_one=1\nfield_two=2\nnote=extra\n", encoding="utf-8")
    result = verify(state, deliverable=target, command=check)
    assert result.ok is False
    assert result.stage == "command"


# --- 7. a strict per-line pattern rejects stray whitespace ------------------------


@POSIX_ONLY
def test_a_strict_key_value_pattern_rejects_a_stray_space_before_the_separator(tmp_path: Path) -> None:
    target = tmp_path / "report.txt"
    state = make_state(tmp_path)
    check = f'! grep -Ev "^[a-z_]+=.+$" {target.name}'

    target.write_text("field_one=1\nfield_two=2\n", encoding="utf-8")
    assert verify(state, deliverable=target, command=check).ok is True

    target.write_text("field_one =1\nfield_two=2\n", encoding="utf-8")
    result = verify(state, deliverable=target, command=check)
    assert result.ok is False
    assert result.stage == "command"


# --- 8. observed runtime behaviour, not a static file -------------------------------


@POSIX_ONLY
def test_a_behaviour_probe_is_checked_the_same_way_as_a_file_check(tmp_path: Path) -> None:
    """The two fix-sqli tasks verify a running service's HTTP responses, not a file's
    bytes. `verify()` has no HTTP-specific branch -- the model's command is expected to
    probe the behaviour itself and report it through the exit code, same as any other
    check. This stands in for that shape with a tiny local script playing "the
    service" instead of a real socket, so the test stays fast and has no network
    dependency; the point is that `verify()`'s command stage does not care what the
    command actually inspected.
    """
    target = tmp_path / "answer.txt"
    target.write_text("patched", encoding="utf-8")
    state = make_state(tmp_path)

    probe = tmp_path / "probe_service.sh"
    probe.write_text(
        "#!/bin/sh\n"
        'if [ "$(cat behaviour.flag)" = "safe" ]; then exit 0; else exit 1; fi\n',
        encoding="utf-8",
    )
    probe.chmod(0o755)
    check = "sh probe_service.sh"

    (tmp_path / "behaviour.flag").write_text("safe", encoding="utf-8")
    assert verify(state, deliverable=target, command=check).ok is True

    (tmp_path / "behaviour.flag").write_text("vulnerable", encoding="utf-8")
    result = verify(state, deliverable=target, command=check)
    assert result.ok is False
    assert result.stage == "command"
