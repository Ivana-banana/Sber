"""Task-specific playbooks and the cheap classifier that picks one.

The design injects exactly one playbook into the system prompt, chosen after
classifying the task text -- never all four. Resident context is paid on every model
call, so at MAX_STEPS a set of four routers would cost tens of thousands of input
tokens per task, the second ranking criterion. One active playbook, <=400 cl100k_base
tokens, keeps that bound (`docs/decisions.md`, 2026-08-25).

The classifier is a keyword heuristic, not an LLM call. A separate routing call would
cost a step and tokens on every task; it is only justified if an ablation shows more
solved tasks (recorded as a hypothesis in `docs/decisions.md`). Until then this is the
baseline router.

The bodies are the accepted M4 draft, distilled from third-party SKILL.md files under
MIT / Apache-2.0. Provenance and license notices live in `THIRD_PARTY_NOTICES.md` at
the repository root; that file ships with the archive whenever these texts do.

The task instruction is untrusted data. Nothing here executes or trusts it -- it is
matched against a fixed keyword table and discarded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TOKEN_BUDGET = 400
"""Resident-context ceiling for a single active playbook, in cl100k_base tokens."""


@dataclass(frozen=True)
class Playbook:
    """One task-shaped procedure, ready to drop into the system prompt."""

    name: str
    body: str
    token_count: int
    """cl100k_base tokens of `body`, measured externally with tiktoken on 2026-08-25.

    tiktoken is not present in the target image, so this is a recorded measurement
    rather than something computed at import time. The line count is what regresses
    locally; see `tests/test_playbooks.py`.
    """


CODE_AUDIT = Playbook(
    name="code-audit",
    token_count=305,
    body="""\
Картируй стек, входы, конфиги и deps; сохрани component map.
Свяжи HTTP, CLI, файлы и env с handlers; сохрани source map.
Картируй SQL, shell, file, URL, template, auth и secrets; сохрани sink map.
Трассируй source→sink и trust boundaries; получи ranked chains.
Отбери полные chains и critical boundaries; сохрани read queue.
Прочитай calls, validation и config вокруг file; сохрани line evidence.
Проверь identity, authz, encoding, params и allowlist; отметь gaps.
Выбери local safe test или trace; сохрани expected signal.
Выполни test; сохрани output, exit code и effect.
Брось chain, если path closed или два cheap tests пусты; получи next chain.
Подтверди effect independent test, log или trace; сохрани second evidence.
Отдели proven findings от suspicions; сохрани два list.
Для finding сохрани path, source→sink, preconditions, effect и evidence.
Повтори external verification line/effect; сохрани PASS или «не подтверждено».
Заверши с verified report; сохрани status.""",
)


CODE_FIX = Playbook(
    name="code-fix",
    token_count=332,
    body="""\
Прочитай contract, исходный diff и limits; сохрани baseline.
Найди code, связанный с symptom; сохрани candidates и lines.
Заморозь tests и evaluator; сохрани diff без их changes.
Создай minimal reproducer; сохрани command и expected failure.
Запусти reproducer до patch; сохрани output, exit code и preconditions.
При пустом result проверь input, version и env; сохрани refined hypothesis.
Брось hypothesis после двух empty reproductions; получи next candidate.
Трассируй data и invariants до cause; сохрани root cause.
Спланируй minimal source patch без смены contract; сохрани scope.
Не меняй и не ослабляй tests, evaluator, expected-answer; сохрани allowed diff.
Внеси patch в cause, не symptom; сохрани small diff.
Повтори reproducer; сохрани исчезновение failure.
Проверь edge cases и security-invariant; сохрани results.
Запусти independent test oracle; сохрани external PASS/FAIL.
Сверь diff с baseline; сохрани отсутствие лишних changes.
При FAIL отбрось hypothesis и локализуй cause; сохрани next step.
Заверши при reproducer и external PASS; сохрани status.""",
)


FORENSICS = Playbook(
    name="forensics",
    token_count=342,
    body="""\
Сохрани артефакты и хеши; получи manifest.
Инвентаризируй файлы, форматы, хосты, интервалы; получи artifact map.
Проверь журналы и утилиты; получи available list.
Не предполагай journalctl, auditd, ausearch; получи «есть/нет».
Определи timezone, точность и gaps; получи time map.
Нормализуй timestamps с originals; получи rule.
Извлеки time, subject, object, action, raw line; получи events.
Разметь auth, process, network, file, privilege; получи groups.
Построй source-tagged timeline; получи event order.
Извлеки IP, account, hash, domain, path, command; получи IOC.
Коррелируй subject, time, sequence; получи links.
Проверь link в primary artifact; сохрани evidence.
Брось hypothesis без двух sources или при time conflict; получи next hypothesis.
Отдели facts от claims; сохрани alternatives.
Сверь timeline другим parser или artifact; получи confirm/conflict.
Укажи IOC type, value, source, interval; получи list.
Дай claim, evidence, alternatives, confidence; получи conclusion.
Понизь confidence при gaps, conflict, one source; сохрани reason.
Проверь artifact reference каждого claim; получи evidence-check.""",
)


CTF = Playbook(
    name="ctf",
    token_count=327,
    body="""\
Инвентаризируй files, services, source, hints и commands; получи surface map.
Определи type и role каждого object; получи object table.
Для binary собери strings, headers, arch, protections; получи profile.
Для service проверь ports, protocol, responses, routes; получи snapshot.
Прочитай config, source, comments, metadata; получи hints.
Разметь logic, web, binary, crypto, forensics, misc; получи ranked hypotheses.
Начни с cheapest reversible hypothesis; сохрани expected signal.
Проверь config, local data, input validation, logic bypass; сохрани results.
Сохрани command, input, output попытки; получи journal.
Сравни response с expected signal; получи verdict.
Брось hypothesis после двух cheap empty checks; получи next hypothesis.
Перед complex chain повтори simple-path search; сохрани triage.
При успехе получи artifact, доказывающий mechanism; сохрани evidence.
Проверь artifact повторным run, service response или unique match; получи external confirmation.
Отдели effect от assumptions и decoys; сохрани confirmations.
Зафиксируй steps, commands, inputs, outputs, conditions; получи reproducible write-up.
Повтори проверку с clean state, если возможно; получи final PASS/FAIL.""",
)


PLAYBOOKS: tuple[Playbook, ...] = (CODE_AUDIT, CODE_FIX, FORENSICS, CTF)

DEFAULT = CODE_FIX
"""Fallback when the task text matches nothing. Repair is the largest announced
category and has the most executable analogues, so an unclassifiable task is least
likely to be hurt by the repair procedure (`docs/decisions.md`, 2026-08-25)."""


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


def select(instruction: str) -> Playbook:
    """Pick the one playbook whose keywords best fit the task text.

    Returns `DEFAULT` when nothing scores. Ties resolve to the first category in
    `_KEYWORDS` order (repair before audit before forensics before ctf), which keeps
    the choice deterministic without an extra rule.
    """
    text = instruction.lower()
    best_name = DEFAULT.name
    best_score = 0
    for name, table in _KEYWORDS.items():
        score = _score(text, table)
        if score > best_score:
            best_score = score
            best_name = name
    return _BY_NAME[best_name]
