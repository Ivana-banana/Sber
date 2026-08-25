"""The playbook set and its cheap classifier.

Two things are worth guarding. The structural invariants keep a body from silently
growing past the resident-context budget the whole design is built around (one
playbook in the prompt, <=400 cl100k_base tokens). The classifier tests pin the
heuristic router: exactly one playbook is chosen from the task text, with no LLM call.
"""

from __future__ import annotations

import pytest

from agent import playbooks


def test_all_four_categories_are_present() -> None:
    names = {pb.name for pb in playbooks.PLAYBOOKS}
    assert names == {"code-audit", "code-fix", "forensics", "ctf"}


@pytest.mark.parametrize("pb", playbooks.PLAYBOOKS, ids=lambda pb: pb.name)
def test_body_stays_within_the_budget(pb: playbooks.Playbook) -> None:
    # The recorded count is an external cl100k_base measurement; tiktoken is not in
    # the target image, so it cannot be recomputed here. The line bound is the real
    # structural guard against a body drifting past the budget in this repo.
    assert pb.token_count <= playbooks.TOKEN_BUDGET
    lines = pb.body.splitlines()
    assert 15 <= len(lines) <= 30, f"{pb.name}: {len(lines)} lines"
    assert all(line.strip() for line in lines), "no blank lines inside a body"


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        # code-fix / repair
        ("Fix the failing tests in /app so the suite passes", "code-fix"),
        ("There is a bug in auth.py causing a traceback; repair it", "code-fix"),
        ("Patch the vulnerability so the exploit no longer works", "code-fix"),
        ("Почини баг в модуле оплаты, тесты должны проходить", "code-fix"),
        # code-audit
        ("Audit this codebase for security vulnerabilities", "code-audit"),
        ("Find all injection vulnerabilities in the API and report them", "code-audit"),
        ("Проведи аудит кода и найди слабые места безопасности", "code-audit"),
        # forensics
        ("Investigate the auth logs and reconstruct the attacker timeline", "forensics"),
        ("Perform log forensics on the incident in /var/log", "forensics"),
        ("Расследуй инцидент по журналам и восстанови timeline", "forensics"),
        # ctf
        ("Solve this CTF challenge and find the flag", "ctf"),
        ("Capture the flag hidden in the binary", "ctf"),
        ("find the flag", "ctf"),
    ],
)
def test_select_routes_representative_instructions(instruction: str, expected: str) -> None:
    assert playbooks.select(instruction).name == expected


def test_select_always_returns_a_playbook() -> None:
    # Untrusted task text may match nothing; the design still injects exactly one.
    chosen = playbooks.select("do the needful with the thing")
    assert isinstance(chosen, playbooks.Playbook)
    assert chosen.name == playbooks.DEFAULT.name


def test_select_is_robust_to_empty_and_odd_input() -> None:
    for weird in ["", "   ", "\n\n", "12345", "-- --"]:
        assert isinstance(playbooks.select(weird), playbooks.Playbook)
