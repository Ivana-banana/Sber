"""Guard: every answer a fixture checker expects must be present in that fixture's environment.

Why this exists. Two extra forensics fixtures (`forensics-ssh-bruteforce`,
`forensics-webshell-chain`) shipped without their log files: `*.log` was ignored by
`.gitignore` when the fixtures were committed. Nothing failed loudly — the tasks simply
became unsolvable, and both candidate arms scored 0/3 on them for reasons that had
nothing to do with the agent. That cost a full measurement of two tasks and was found
only by reading traces.

The check is deliberately narrow, because a broad one is noise. It looks only at answers
a checker states verbatim:

* `key=value` lines of `tests/expected*` files (the forensic report form), and
* string values of a dict named `expected*` in `tests/check.py`.

A value that appears nowhere under `environment/` cannot be derived by any agent, so it
is reported as unreachable. The reverse does not hold: a value being present does not
make a task solvable, and tasks whose answer is computed at runtime (code fixes, CTF
flags) state no verbatim answer at all and are therefore invisible here.

Usage: python3 scripts/eval/check_fixture_answers.py [tasks-root ...]
Exit code 1 if some expected answer is unreachable, 0 otherwise.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ROOTS = ("local_task/tasks", "local_task_extra/tasks", "local_task")
MIN_LENGTH = 3


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def expected_from_report(text: str) -> set[str]:
    """Right-hand sides of the `key=value` lines of an expected deliverable."""
    values: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if line.count("=") >= 1 and not line.startswith("="):
            values.add(line.split("=", 1)[1].strip())
    return values


def expected_from_checker(source: str) -> set[str]:
    """String values of a dict the checker binds to a name containing `expected`."""
    values: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return values
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        names = [t.id.lower() for t in node.targets if isinstance(t, ast.Name)]
        if not any("expected" in name for name in names):
            continue
        for item in node.value.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                values.add(item.value.strip())
    return values


def environment_text(task: Path) -> str:
    env = task / "environment"
    if not env.is_dir():
        return ""
    return "".join(_read(f) for f in sorted(env.rglob("*")) if f.is_file())


def unreachable(task: Path) -> list[str]:
    tests = task / "tests"
    if not tests.is_dir():
        return []
    values: set[str] = set()
    for path in sorted(tests.iterdir()):
        if not path.is_file():
            continue
        if path.name.startswith("expected"):
            values |= expected_from_report(_read(path))
        elif path.name == "check.py":
            values |= expected_from_checker(_read(path))
    haystack = environment_text(task)
    return sorted(v for v in values if len(v) >= MIN_LENGTH and v not in haystack)


def tasks_under(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [p for p in sorted(root.iterdir()) if (p / "task.toml").is_file()]


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv[1:]] or [ROOT / r for r in DEFAULT_ROOTS]
    seen: set[Path] = set()
    checked = 0
    broken = 0
    for root in roots:
        for task in tasks_under(root):
            resolved = task.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            checked += 1
            missing = unreachable(task)
            if missing:
                broken += 1
                print(f"UNREACHABLE {task.name}")
                for value in missing:
                    print(f"    {value!r} appears in no file under environment/")
    print(f"checked {checked} fixtures, {broken} with an unreachable expected answer")
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
