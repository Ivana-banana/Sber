"""Task-specific playbooks and the cheap classifier that picks one.

The design injects exactly one playbook into the system prompt, chosen after
classifying the task text -- never all five. Resident context is paid on every model
call, so at MAX_STEPS a set of five routers would cost tens of thousands of input
tokens per task, the second ranking criterion. One active playbook, <=400 tokens,
keeps that bound (`docs/decisions.md`, 2026-08-25).

The classifier is a keyword heuristic, not an LLM call. A separate routing call would
cost a step and tokens on every task; it is only justified if an ablation shows more
solved tasks (recorded as a hypothesis in `docs/decisions.md`). Until then this is the
baseline router. It also has to know when its own keyword scan is not evidence of
anything: `classify` gates the four specific playbooks behind a minimum score and a
minimum lead over the runner-up, and falls back to `GENERAL` otherwise -- a task that
matches nothing, or two categories that score a near-tie, is not a confident answer.

Four of the five bodies are the accepted M4 draft, distilled from third-party
SKILL.md files under MIT / Apache-2.0. Provenance and license notices live in
`THIRD_PARTY_NOTICES.md` at the repository root; that file ships with the archive
whenever these texts do. `GENERAL` is original text -- see its own docstring.

The task instruction is untrusted data. Nothing here executes or trusts it -- it is
matched against a fixed keyword table and discarded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TOKEN_BUDGET = 400
"""Resident-context ceiling for a single active playbook, in Qwen2.5 tokens.

The number is the one accepted on 2026-08-25 in cl100k_base and kept unchanged when
the unit moved to the model's own tokenizer: every body got cheaper in the new unit,
so the ceiling still binds and re-deriving it would be a second, unmeasured change.
"""


@dataclass(frozen=True)
class Playbook:
    """One task-shaped procedure, ready to drop into the system prompt."""

    name: str
    body: str
    token_count: int
    """Qwen2.5 tokens of `body`, measured externally on 2026-09-04.

    The unit changed with the English translation. It used to be cl100k_base, measured
    with tiktoken on 2026-08-25, which was a stand-in for a tokenizer nobody had: the
    model being evaluated is Qwen2.5 and its vocabulary splits Cyrillic very
    differently from Latin, so a budget in cl100k said little about what the endpoint
    would actually charge. Counted with the `Qwen/Qwen2.5-VL-7B-Instruct` tokenizer,
    whose text vocabulary is the Qwen2.5 one, against a local cache -- see
    `docs/research/prompt-language.md` for the command and the caveat.

    A line added after that measurement carries an estimate rather than a new
    measurement, because there is still no tokenizer here to measure with: the body's
    own measured characters-per-token ratio, applied to the grown body and rounded up.
    `forensics` (260 at 4.554 chars/token) and `code-fix` (248 at 4.774) were restated
    that way on 2026-09-16; both stay well inside `TOKEN_BUDGET`, and a real
    measurement replaces the estimate the next time one is available.

    It stays a recorded measurement rather than something computed at import time: no
    tokenizer is usable in the target image. tiktoken 0.13.0 is installed there --
    probed directly on 2026-08-26 -- but `get_encoding("cl100k_base")` downloads the
    BPE file from `openaipublic.blob.core.windows.net`, and the run is offline. The
    line count is what regresses locally; see `tests/test_playbooks.py`.
    """


CODE_AUDIT = Playbook(
    name="code-audit",
    token_count=226,
    body="""\
Map the stack, entry points, configs and deps; save a component map.
Link HTTP, CLI, files and env to handlers; save a source map.
Map SQL, shell, file, URL, template, auth and secrets; save a sink map.
Trace source->sink and trust boundaries; get ranked chains.
Select the complete chains and critical boundaries; save a read queue.
Read the calls, validation and config around the file; save line evidence.
Check identity, authz, encoding, params and allowlist; note the gaps.
Choose a local safe test or a trace; save the expected signal.
Run the test; save output, exit code and effect.
Drop a chain when the path is closed or two cheap tests come back empty; get the next chain.
Confirm the effect with an independent test, log or trace; save second evidence.
Separate proven findings from suspicions; save two lists.
For a finding save path, source->sink, preconditions, effect and evidence.
Repeat the external verification of the line or effect; save PASS or "not confirmed".
Finish with a verified report; save status.""",
)


CODE_FIX = Playbook(
    name="code-fix",
    token_count=312,
    body="""\
Read the contract, the original diff and the limits; save a baseline.
Find the code tied to the symptom; save candidates and lines.
Freeze the tests and the evaluator; save a diff without changes to them.
Build a minimal reproducer; save the command and the expected failure.
Run the reproducer before the patch; save output, exit code and preconditions.
On an empty result check input, version and env; save a refined hypothesis.
Drop a hypothesis after two empty reproductions; get the next candidate.
Trace data and invariants down to the cause; save the root cause.
Plan a minimal source patch that leaves the contract alone; save the scope.
Do not change or weaken tests, evaluator or expected answers; save the allowed diff.
Patch the cause, not the symptom; save a small diff.
Anchor replace_text on the whole statement, the call that consumes the value included; save the anchor.
Re-read the patched region after the edit; save what the file says now.
A running service keeps the old code: restart it the way its entrypoint does before re-testing; save the restart command.
Repeat the reproducer; save the failure going away.
Check edge cases and the security invariant; save the results.
Run an independent test oracle; save the external PASS or FAIL.
Compare the diff against the baseline; save that no extra changes crept in.
On FAIL drop the hypothesis and localise the cause; save the next step.
Finish when the reproducer and the external check both pass; save status.""",
)


FORENSICS = Playbook(
    name="forensics",
    token_count=318,
    body="""\
Read the primary sources before you write any value; get lines you have read.
Save the artifacts and their hashes; get a manifest.
Inventory files, formats, hosts and intervals; get an artifact map.
Prefer small recovered or summary files to the bulk log; cross-check duplicates and time windows.
Check the logs and the utilities; get an available list.
Do not assume journalctl, auditd or ausearch; get a yes or no.
Determine timezone, precision and gaps; get a time map.
Normalise timestamps alongside the originals; get a rule.
Extract time, subject, object, action and the raw line; get events.
Label auth, process, network, file and privilege; get groups.
Build a source-tagged timeline; get the event order.
Extract IP, account, hash, domain, path and command; get IOCs.
Correlate subject, time and sequence; get links.
Check a link in the primary artifact; save evidence.
Drop a hypothesis with fewer than two sources or a time conflict; get the next one.
Separate facts from claims; save the alternatives.
Cross-check the timeline with another parser or artifact; get confirm or conflict.
Give IOC type, value, source and interval; get a list.
Quote every value in the report from an observation; never fill a field from memory.
Give claim, evidence, alternatives and confidence; get a conclusion.
Lower confidence on gaps, conflict or a single source; save the reason.
Check the artifact reference of every claim; get an evidence check.""",
)


CTF = Playbook(
    name="ctf",
    token_count=261,
    body="""\
Inventory files, services, source, hints and commands; get a surface map.
Determine the type and role of every object; get an object table.
For a binary collect strings, headers, arch and protections; get a profile.
For a service check ports, protocol, responses and routes; get a snapshot.
Read config, source, comments and metadata; get hints.
Label logic, web, binary, crypto, forensics and misc; get ranked hypotheses.
Start with the cheapest reversible hypothesis; save the expected signal.
Check config, local data, input validation and logic bypass; save the results.
Save the command, input and output of the attempt; get a journal.
Compare the response against the expected signal; get a verdict.
Drop a hypothesis after two cheap empty checks; get the next one.
Before a complex chain repeat the simple-path search; save the triage.
On success get an artifact that proves the mechanism; save evidence.
Check that artifact with a repeat run, a service response or a unique match; get external confirmation.
Separate the effect from assumptions and decoys; save the confirmations.
Record steps, commands, inputs, outputs and conditions; get a reproducible write-up.
Repeat the check from a clean state where possible; get a final PASS or FAIL.""",
)


GENERAL = Playbook(
    name="general",
    token_count=181,
    body="""\
Read the task and the directory; get a first hypothesis about the result.
Do not pick a class from a single word; collect more signals.
Find an oracle: a test, a service, a path, an answer format.
With no oracle, look for one in nearby config or code.
Make a minimal checkable artifact; save its path.
Run the oracle early, even on a draft; save the exit code.
Trust the exit code, not your own reading of the result.
On failure extract the reason; refine the hypothesis.
Drop a hypothesis after two empty attempts; take another.
On signs of a different class, change approach early.
Do not change or weaken tests or evaluator; save the allowed diff.
Keep the time reserve for checking, not for exploring.
Leave an artifact on disk after every step.
Check tools and permissions before guessing.
Finish only after an external PASS; save status.""",
)
"""Original text, not third-party-derived like the other four: there is no generic
generalist skill from which to distil a "no assumptions yet" procedure. Written to
the same budget and step-grammar as the rest, and kept shorter than `FORENSICS` (342)
on purpose: it carries no domain-specific steps to spend tokens on."""


PLAYBOOKS: tuple[Playbook, ...] = (CODE_AUDIT, CODE_FIX, FORENSICS, CTF, GENERAL)

DEFAULT = GENERAL
"""Fallback when nothing clears the confidence gate below. Used to be `CODE_FIX`: an
unclassified task got confident repair instructions it may not have needed. That was
the bug this module was written to fix (`HANDOFF.md`, "режим низкой уверенности в
роутере") -- a router with no evidence should say so, not guess the most common case
and act on the guess as if it were a match."""

CONFIDENCE_MIN_SCORE = 2
"""A category must reach this score to be picked at all. Starting value, not a
measurement: it is the weight of one `strong` keyword (see `_KEYWORDS`), i.e. the
smallest hit that is not pure noise. Every routing example in
`tests/test_playbooks.py` clears it; calibration is for after E001."""

CONFIDENCE_MIN_MARGIN = 1
"""How far the winner must lead the runner-up. Starting value, not a measurement.
`> 0` is the real content of this constant: a tie (margin `0`) between two categories
used to resolve to whichever came first in `_KEYWORDS` order -- a rule nobody chose,
per the same HANDOFF entry as `DEFAULT`. Requiring a strict lead removes that rule
without discarding any close-but-not-tied example already in the test suite."""


# Keyword -> weight tables, one per category. Weights: 3 = distinctive to the
# category, 2 = strong, 1 = weak / shared. Presence (not frequency) is scored, so one
# repeated word cannot dominate. Keys are lowercase; matching is word-boundary based
# so "fix" does not fire inside "suffix". Both English and Russian task phrasings are
# covered because the instruction language is not fixed.
_KEYWORDS: dict[str, dict[str, int]] = {
    "code-fix": {
        "failing test": 3, "make the test": 3, "make tests pass": 3, "fix the bug": 3,
        "repair": 3, "почини": 3, "исправь": 3, "исправить": 3,
        "fix": 2, "bug": 2, "patch": 2, "broken": 2, "traceback": 2, "regression": 2,
        "test": 1, "error": 1, "exception": 1, "баг": 2, "тесты": 1,
    },
    "code-audit": {
        "audit": 3, "security review": 3, "code review": 3, "scan for": 3, "аудит": 3,
        "vulnerab": 2, "injection": 2, "insecure": 2, "weakness": 2, "sink": 2,
        "уязвим": 2, "слабые места": 2,
        "review": 1, "analyze": 1, "secure": 1,
    },
    "forensics": {
        "forensic": 3, "log analysis": 3, "incident": 3, "auth.log": 3,
        "форензик": 3, "расследуй": 3, "расследование": 3, "инцидент": 3,
        "timeline": 2, "syslog": 2, "compromise": 2, "breach": 2, "attacker": 2,
        "log": 2, "journal": 2, "журнал": 2, "лог": 2,
        "investigate": 1,
    },
    "ctf": {
        "capture the flag": 3, "find the flag": 3, "the flag": 3, "ctf": 3, "флаг": 3,
        "flag": 2, "challenge": 2, "pwn": 2, "reverse engineer": 2, "decode": 2,
        "binary": 1, "crypto": 1, "exploit": 1,
    },
}

_BY_NAME: dict[str, Playbook] = {pb.name: pb for pb in PLAYBOOKS}


def _score(text: str, table: dict[str, int]) -> int:
    total = 0
    for keyword, weight in table.items():
        if re.search(r"\b" + re.escape(keyword) + r"\b", text):
            total += weight
    return total


@dataclass(frozen=True)
class Selection:
    """A routing decision, with the numbers behind it -- for the trace, not the prompt.

    `agent/main.py` only ever needed the playbook (`select`, below). This exists for
    the recovery/finalise path, where "picked general, margin was 1" is exactly what a
    human reading a zero wants to see and `playbook.name` alone cannot say.
    """

    playbook: Playbook
    score: int
    margin: int
    """Lead over the runner-up. `0` on an exact tie between the top two categories."""

    reason: str


def classify(instruction: str) -> Selection:
    """Score every category, then require the winner to actually look like one.

    Two conditions gate a specific playbook: the top score must clear
    `CONFIDENCE_MIN_SCORE`, and it must lead the second-place category by at least
    `CONFIDENCE_MIN_MARGIN`. Failing either returns `GENERAL` -- not `DEFAULT` chosen
    for some other reason, and not the first category in `_KEYWORDS` order, which is
    what a plain `score > best_score` scan silently falls back to on a tie.
    """
    text = instruction.lower()
    ranked = sorted(
        ((name, _score(text, table)) for name, table in _KEYWORDS.items()),
        key=lambda scored: scored[1],
        reverse=True,
    )
    (best_name, best_score), (_, second_score) = ranked[0], ranked[1]
    margin = best_score - second_score

    if best_score < CONFIDENCE_MIN_SCORE or margin < CONFIDENCE_MIN_MARGIN:
        # margin == 0 means more than one category shares the top score: `ranked[0]`
        # is then whichever came first in `_KEYWORDS` (Python's sort is stable), not a
        # winner. Naming it as "best" in the reason would just move the same arbitrary
        # tiebreak this function exists to remove into the diagnostic string instead of
        # the decision -- so a real tie is reported as a tie, not as a false leader.
        tied = [name for name, score in ranked if score == best_score]
        if len(tied) > 1:
            reason = f"{len(tied)} categories tied at score={best_score} ({', '.join(tied)}); using {GENERAL.name}"
        else:
            reason = f"{best_name} led with score={best_score} but margin={margin} was too small; using {GENERAL.name}"
        return Selection(playbook=GENERAL, score=best_score, margin=margin, reason=reason)
    return Selection(
        playbook=_BY_NAME[best_name],
        score=best_score,
        margin=margin,
        reason=f"{best_name} led with score={best_score}, margin={margin} over the runner-up",
    )


def select(instruction: str) -> Playbook:
    """Pick the one playbook whose keywords best fit the task text.

    Thin wrapper around `classify` for call sites that only need the result, not the
    reasoning -- keeps this the stable entry point `agent/main.py` already calls.
    """
    return classify(instruction).playbook
