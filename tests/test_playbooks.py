"""The playbook set and its cheap classifier.

Two things are worth guarding. The structural invariants keep a body from silently
growing past the resident-context budget the whole design is built around (one
playbook in the prompt, <=400 cl100k_base tokens). The classifier tests pin the
heuristic router: exactly one playbook is chosen from the task text, with no LLM call.
"""

from __future__ import annotations

import pytest

from agent import playbooks


def test_all_five_categories_are_present() -> None:
    names = {pb.name for pb in playbooks.PLAYBOOKS}
    assert names == {"code-audit", "code-fix", "forensics", "ctf", "general"}


@pytest.mark.parametrize("pb", playbooks.PLAYBOOKS, ids=lambda pb: pb.name)
def test_body_stays_within_the_budget(pb: playbooks.Playbook) -> None:
    # The recorded count is an external cl100k_base measurement; tiktoken is not in
    # the target image, so it cannot be recomputed here. The line bound is the real
    # structural guard against a body drifting past the budget in this repo.
    assert pb.token_count <= playbooks.TOKEN_BUDGET
    lines = pb.body.splitlines()
    assert 15 <= len(lines) <= 30, f"{pb.name}: {len(lines)} lines"
    assert all(line.strip() for line in lines), "no blank lines inside a body"


def test_general_stays_shorter_than_the_previous_longest_playbook() -> None:
    # agent/prompt.py's resident-prompt arithmetic was measured against the old
    # ceiling (FORENSICS, 342). GENERAL must stay under it, not just under
    # TOKEN_BUDGET, or that measurement is stale the moment this body changes.
    longest_specific = max(pb.token_count for pb in playbooks.PLAYBOOKS if pb is not playbooks.GENERAL)
    assert playbooks.GENERAL.token_count < longest_specific


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


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        # Inflected forms: a stem keyword used to require the bare stem as a whole word,
        # so "vulnerability", "forensics" or "уязвимость" scored nothing.
        ("Look for a vulnerability in the upload handler", "code-audit"),
        ("Run forensics on the collected files", "forensics"),
        ("Найди уязвимость в обработчике загрузки", "code-audit"),
        ("Нужна форензика по собранным файлам", "forensics"),
        # A review whose deliverable is a list of findings, never naming "audit".
        ("Review the payment module without modifying it and write findings.json with a findings array", "code-audit"),
        ("Assess the gateway source; report each weakness as a finding in report.json. Do not change the code.", "code-audit"),
        # Forensics described through its evidence and output keys, not the word itself.
        ("Read every file in /data/case and write report.txt with attacker_ip and compromised_user lines", "forensics"),
        ("Go through the evidence under /srv/evidence and give the exfiltrated byte count and the source address", "forensics"),
        # "audit" as a kind of log record is evidence, not a request for a code audit.
        ("Match each audit record with the proxy log and write the attacker address to out.txt", "forensics"),
        ("Correlate the export audit events with the edge decisions; write the principal to result.env", "forensics"),
        # A repair whose title says "incident" is still a repair.
        ("Billing cache incident: fix the tenant leak in /app/cache.py and keep the public API", "code-fix"),
        ("Repair the lookup in /srv/app/store.py so crafted input cannot bypass the passcode", "code-fix"),
        ("Review /srv/app/filter.py and fix match(q) without changing its signature", "code-fix"),
        ("Which account's session carried the largest egress? Copy its timestamp into answer.env", "forensics"),
    ],
)
def test_select_routes_by_what_the_task_asks_not_its_exact_wording(instruction: str, expected: str) -> None:
    assert playbooks.select(instruction).name == expected


def test_select_always_returns_a_playbook() -> None:
    # Untrusted task text may match nothing; the design still injects exactly one.
    chosen = playbooks.select("do the needful with the thing")
    assert isinstance(chosen, playbooks.Playbook)
    assert chosen.name == playbooks.DEFAULT.name


def test_select_is_robust_to_empty_and_odd_input() -> None:
    for weird in ["", "   ", "\n\n", "12345", "-- --"]:
        assert isinstance(playbooks.select(weird), playbooks.Playbook)


# --- confidence gate ---------------------------------------------------------


def test_at_least_two_keyword_categories_exist() -> None:
    # classify() unpacks the top two ranked categories unconditionally; it silently
    # assumes this. Pin it the same way test_all_five_categories_are_present pins
    # PLAYBOOKS, so a future edit to _KEYWORDS fails loudly here instead of an
    # IndexError three functions away.
    assert len(playbooks._KEYWORDS) >= 2


def test_a_task_with_no_keyword_hits_falls_back_to_general_not_code_fix() -> None:
    # The router used to default an unclassified task straight to CODE_FIX -- a
    # specific repair procedure with no evidence behind it. That was the bug.
    decision = playbooks.classify("do the needful with the thing")
    assert decision.playbook.name == "general"
    assert decision.score == 0
    # All four categories score 0 here, a genuine 4-way tie, not one arbitrary
    # "best" hiding behind the fallback -- the reason must say so honestly rather
    # than claiming a single category led.
    assert "tied" in decision.reason
    assert "led" not in decision.reason, "no single category is a leader in a 4-way tie"


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        ("Почини баг в модуле оплаты", "code-fix"),
        ("Проведи аудит кода на уязвимости", "code-audit"),
        ("Расследуй инцидент по журналам", "forensics"),
        ("Найди флаг в этом бинарнике", "ctf"),
    ],
)
def test_one_explicit_keyword_per_category_routes_confidently(instruction: str, expected: str) -> None:
    decision = playbooks.classify(instruction)
    assert decision.playbook.name == expected
    assert decision.score >= playbooks.CONFIDENCE_MIN_SCORE
    assert decision.margin >= playbooks.CONFIDENCE_MIN_MARGIN


def test_a_near_tie_between_two_categories_falls_back_to_general_not_dict_order() -> None:
    # "bug" (code-fix, weight 2) and "insecure" (code-audit, weight 2) fire equally.
    # The old scan picked whichever category iterates first in `_KEYWORDS` -- a rule
    # nobody chose. This must not silently resolve to code-fix just because code-fix
    # is the first key in the dict.
    instruction = "There is a bug in the insecure module"
    scores = {name: playbooks._score(instruction.lower(), table) for name, table in playbooks._KEYWORDS.items()}
    assert scores["code-fix"] == scores["code-audit"] > 0, "fixture assumption: the two must tie"

    decision = playbooks.classify(instruction)

    assert decision.playbook.name == "general"
    assert decision.margin == 0
    assert "2 categories tied" in decision.reason
    assert "code-fix" in decision.reason and "code-audit" in decision.reason
    assert "led" not in decision.reason, "a tie has no single leader to name"


def test_classify_reports_the_winning_margin_for_a_confident_case() -> None:
    decision = playbooks.classify("Solve this CTF challenge and find the flag")
    assert decision.playbook.name == "ctf"
    assert decision.margin > 0
    assert "ctf" in decision.reason


def test_select_is_classify_without_the_reasoning() -> None:
    for instruction in ["fix the bug", "audit for injection", "do the needful", ""]:
        assert playbooks.select(instruction) == playbooks.classify(instruction).playbook


# --- the two lines each measured failure asked for ---------------------------


def test_the_forensic_playbook_demands_the_sources_before_any_value() -> None:
    # Six recorded episodes of a log investigation wrote a report without reading one
    # of the seven files that held the answer, and every value in all six was invented.
    body = playbooks.select("Investigate the incident logs and reconstruct the timeline").body

    assert "Read the primary sources before you write any value" in body
    assert "never fill a field from memory" in body


def test_the_forensic_playbook_prefers_the_small_artifacts_to_the_bulk_log() -> None:
    body = playbooks.FORENSICS.body

    assert "Prefer small recovered or summary files to the bulk log" in body
    assert "cross-check duplicates and time windows" in body


def test_the_code_fix_playbook_anchors_an_edit_on_the_whole_statement() -> None:
    # Twelve recorded episodes of two repair tasks: every executed edit was in the
    # right file and the right function, and the anchor decided the outcome. Four of
    # four succeeded when it covered the call that consumes the value; five of five
    # failed when it stopped at the value itself, leaving a stale or duplicated call.
    body = playbooks.select("Fix the bug so the failing test passes").body

    assert "Anchor replace_text on the whole statement" in body
    assert "the call that consumes the value" in body
    assert "Re-read the patched region after the edit" in body


def test_the_new_playbook_lines_name_no_public_task() -> None:
    for pb in playbooks.PLAYBOOKS:
        lowered = pb.body.lower()
        for name in ("incident-log-forensics", "app.jsonl", "auth.log", "sqli", "fetchrow"):
            assert name not in lowered, f"{pb.name} would be overfitting to the public set"
