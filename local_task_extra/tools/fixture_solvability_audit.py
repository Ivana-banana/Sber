"""Offline check that a fixture's expected answer is derivable from its environment.

Why this exists. `run_reference_suite.sh` runs `solution/solve.sh` against
`tests/test.sh`, and `solve.sh` simply prints the expected answer. That proves the
checker agrees with the reference answer; it says nothing about whether the answer can
be found in the files the agent is given. The two are different questions, and only the
first one was ever asked: `forensics-webshell-chain` and `forensics-ssh-bruteforce`
shipped with their evidence missing (`*.log` was in `.gitignore` at the time), the
reference suite still reported 18/18, and three days of extra-set measurement counted
two tasks that no agent could have solved.

What this checks. For every task whose expected answer is a set of literal values -- a
`tests/expected_*.txt` in `key=value` form, or an `expected`/`exp` dict literal in
`tests/check.py` -- every value must appear somewhere under `environment/`. Tasks whose
answer is a behaviour (the `fix-*`, `ctf-*` and most `audit-*` packages) have no literal
to look for and are reported as skipped, not as passed.

What this does NOT check: that the value is findable by a *reasonable* amount of work,
that distractors exist, or that the task is solvable by any particular model. A value
present in the environment can still be buried; a value absent from it cannot be found
at all. This is the second question only.

Usage:  python3 local_task_extra/tools/fixture_solvability_audit.py [tasks-root]
Exit 0 when nothing is missing, 1 otherwise.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "tasks"

EXPECTED_DICT_NAMES = frozenset({"expected", "exp"})
"""Names the shipped checkers bind their answer dict to. Kept narrow on purpose: a
wider sweep would pick up unrelated literals and turn this audit into noise."""

DERIVED_VALUES: dict[str, frozenset[str]] = {}
"""Task -> values that are legitimately absent from `environment/` because the agent is
meant to compute them (a sum, a difference, a decoded form) rather than copy them.

Empty today: every current fixture states its answer verbatim somewhere in the
evidence. An entry here needs one line saying how the value is derived -- otherwise it
is indistinguishable from the defect this audit exists to catch."""


def expected_values(task: Path) -> set[str]:
    """Literal answer values declared by a task's tests, or an empty set."""
    values: set[str] = set()
    tests = task / "tests"
    for expected_file in sorted(tests.glob("expected_*.txt")):
        for line in expected_file.read_text().splitlines():
            if "=" in line:
                values.add(line.split("=", 1)[1].strip())
    check = tests / "check.py"
    if check.is_file():
        try:
            tree = ast.parse(check.read_text())
        except SyntaxError:
            return values
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
                continue
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if not names & EXPECTED_DICT_NAMES:
                continue
            for value in node.value.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    values.add(value.value)
    return {v for v in values if v}


def environment_text(task: Path) -> str:
    """Everything the agent can read, concatenated. Unreadable bytes are skipped
    rather than fatal: a fixture may legitimately ship a binary artifact."""
    env = task / "environment"
    if not env.is_dir():
        return ""
    return "\n".join(
        path.read_text(errors="ignore") for path in sorted(env.rglob("*")) if path.is_file()
    )


def missing_values(task: Path) -> list[str]:
    """Expected values that appear nowhere under `environment/`."""
    allowed = DERIVED_VALUES.get(task.name, frozenset())
    blob = environment_text(task)
    return sorted(v for v in expected_values(task) - allowed if v not in blob)


def audit(root: Path) -> tuple[dict[str, list[str]], list[str]]:
    """(task -> missing values, names skipped for having no literal answer)."""
    failures: dict[str, list[str]] = {}
    skipped: list[str] = []
    for task in sorted(p for p in root.iterdir() if (p / "task.toml").is_file()):
        if not expected_values(task):
            skipped.append(task.name)
            continue
        missing = missing_values(task)
        if missing:
            failures[task.name] = missing
    return failures, skipped


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else DEFAULT_ROOT
    failures, skipped = audit(root)
    packages = [p for p in root.iterdir() if (p / "task.toml").is_file()]
    checked = len(packages) - len(skipped)
    for name, missing in failures.items():
        print(f"{name}: expected values not present in environment/: {missing}", file=sys.stderr)
    if failures:
        print(f"SOLVABILITY_FAIL tasks={len(failures)} checked={checked}", file=sys.stderr)
        return 1
    print(f"SOLVABILITY_PASS checked={checked} skipped_behavioural={len(skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
