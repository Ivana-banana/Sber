"""Guard: a fixture must be solvable and must survive a fresh checkout.

Why this exists. Two extra forensics fixtures (`forensics-ssh-bruteforce`,
`forensics-webshell-chain`) shipped without their log files: `*.log` was ignored by
`.gitignore` when the fixtures were committed. Nothing failed loudly — the tasks simply
became unsolvable, both candidate arms scored 0/3 on them for reasons that had nothing
to do with the agent, and `local_task_extra/validation/reference-results.tsv` still said
`PASS reward=1`, because the reference solution writes the answer as a constant and never
reads the evidence. The defect was found by reading traces, days later.

Two independent checks, because the first one alone missed the way it happened:

1. **Reachability.** Every answer a checker states verbatim must appear somewhere under
   `environment/`. Verbatim answers are the `key=value` lines of `tests/expected*` files
   and the string values of dict literals in `tests/*.py`. A value that appears in no
   evidence file cannot be derived by any agent.
2. **Tracking.** Every file under a fixture tree must be tracked by git. This is what
   actually broke: the file existed on the author's disk, `.gitignore` swallowed it, and
   the fixture was already unsolvable when it reached the repository.

What this does **not** prove. A present value does not make a task solvable, and a task
whose answer is computed at run time (a hash, a sum, a code fix, a CTF flag) states no
verbatim answer at all and is invisible to check 1 — the report says how many fixtures
that covers instead of pretending they were verified. Reachability is also blind to a
`Dockerfile` that copies an explicit file list: the evidence can be in the repository and
still be absent from the image.

Usage: python3 scripts/eval/check_fixture_answers.py [--show-values] [tasks-root ...]
Exit 1 if an answer is unreachable or a fixture file is untracked, 2 if nothing was
checked at all, 0 otherwise.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ROOTS = ("local_task", "local_task_extra/tasks")
ANSWER_NAMES = ("expected", "exp", "want", "answer", "golden")
# Two characters collide with everything: `ok`, `no`, a one-letter verdict. Reporting
# them as reachable would be luck, not evidence, so they are left out of check 1.
MIN_LENGTH = 3
WORD = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


class Warned(list):
    """Problems that must be visible but do not decide the exit code on their own."""


def read_text(path: Path, warnings: Warned) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        warnings.append(f"unreadable, treated as empty: {path} ({exc.strerror})")
        return ""


def key_value_answers(text: str) -> set[str]:
    """Right-hand sides of `key=value` lines, ignoring prose that merely contains `=`."""
    answers: set[str] = set()
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not value or not key:
            continue
        if not key[0].isalpha() and key[0] != "_":
            continue
        if any(c not in WORD and c not in ".-" for c in key):
            continue
        answers.add(value)
    return answers


def json_answers(text: str) -> set[str]:
    try:
        loaded = json.loads(text)
    except ValueError:
        return set()
    answers: set[str] = set()
    stack = [loaded]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            answers.add(item.strip())
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return answers


def checker_answers(source: str, warnings: Warned, where: Path) -> set[str]:
    """String values of dict literals a checker compares the deliverable against.

    The dict is recognised by the name it is bound to, and the four forensics checkers
    in this repository use four different ones (`expected` once, `exp` three times), so
    the list below is wider than a single spelling. Anonymous dict literals are left
    alone on purpose: in `local_task/fix-sqli-login/tests` they carry attack payloads
    that are meant to be absent from the environment, and counting them would turn a
    healthy fixture red.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        warnings.append(f"unparsable checker, no answers taken from it: {where} ({exc.msg})")
        return set()

    named: set[int] = set()
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Dict):
            if any(part in target.id.lower() for part in ANSWER_NAMES):
                named.add(id(node.value))

    answers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict) or not node.values:
            continue
        if id(node) not in named:
            continue
        values = [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        if len(values) == len(node.values):
            answers.update(v.strip() for v in values)
    return answers


def expected_answers(task: Path, warnings: Warned) -> set[str]:
    tests = task / "tests"
    if not tests.is_dir():
        return set()
    answers: set[str] = set()
    for path in sorted(tests.rglob("*")):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name.startswith("expected"):
            text = read_text(path, warnings)
            answers |= json_answers(text) if name.endswith(".json") else key_value_answers(text)
        elif path.suffix == ".py":
            answers |= checker_answers(read_text(path, warnings), warnings, path)
    return {a for a in answers if len(a) >= MIN_LENGTH}


def evidence(task: Path, warnings: Warned) -> str:
    """Text of every file under `environment/`, newline-joined.

    The separator is not cosmetic: concatenating the files directly invents matches
    across the seam, and the report would then claim a value appears in a file when it
    appears in no file.
    """
    env = task / "environment"
    if not env.is_dir():
        return ""
    return "\n".join(read_text(f, warnings) for f in sorted(env.rglob("*")) if f.is_file())


def reachable(value: str, haystack: str) -> bool:
    """True when the value appears as a whole token, not inside a longer one.

    `198.51.100.44` must not be satisfied by `198.51.100.440`, and `deploy` must not be
    satisfied by the word `deployment` in a README.
    """
    start = haystack.find(value)
    while start != -1:
        before = haystack[start - 1] if start else ""
        after = haystack[start + len(value) : start + len(value) + 1]
        if before not in WORD and after not in WORD:
            return True
        start = haystack.find(value, start + 1)
    return False


def show(value: str, reveal: bool) -> str:
    if reveal:
        return repr(value)
    return f"{value[:2]}…{value[-1:]} ({len(value)} chars)"


def tasks_under(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.iterdir()):
        if (path / "task.toml").is_file():
            found.append(path)
    return found


def untracked_files(root: Path) -> list[Path] | None:
    """Files under `root` that git does not track, or None when git cannot answer."""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", str(root)],
            cwd=root if root.is_dir() else root.parent,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    tracked = {
        (root / name).resolve()
        for name in listed.stdout.decode("utf-8", "replace").split("\0")
        if name
    }
    if not tracked:
        return None
    # Byte-code caches are the one thing under a fixture tree that is untracked on
    # purpose: `.gitignore` re-excludes them right after re-including everything else,
    # and running a checker locally creates them. Reporting them would make the guard
    # red on a developer's machine for a reason that cannot break a fixture.
    on_disk = {
        p.resolve()
        for p in root.rglob("*")
        if p.is_file() and p.suffix not in (".pyc", ".pyo") and "__pycache__" not in p.parts
    }
    return sorted(on_disk - tracked)


def main(argv: list[str]) -> int:
    reveal = "--show-values" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    roots = [Path(a) for a in args] or [ROOT / r for r in DEFAULT_ROOTS]

    warnings = Warned()
    seen: set[Path] = set()
    checked = 0
    stateless = 0
    broken = 0

    for root in roots:
        if not root.is_dir():
            warnings.append(f"no such tasks root, skipped: {root}")
            continue
        for path in sorted(root.iterdir()):
            if path.is_dir() and (path / "tests").is_dir() and not (path / "task.toml").is_file():
                warnings.append(f"looks like a task but has no task.toml, skipped: {path}")
        for task in tasks_under(root):
            resolved = task.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            checked += 1
            answers = expected_answers(task, warnings)
            if not answers:
                stateless += 1
                continue
            haystack = evidence(task, warnings)
            missing = sorted(a for a in answers if not reachable(a, haystack))
            if missing:
                broken += 1
                print(f"UNREACHABLE {task.name}")
                for value in missing:
                    print(f"    {show(value, reveal)} appears in no file under environment/")

        untracked = untracked_files(root)
        if untracked is None:
            warnings.append(f"git could not list tracked files under {root}, tracking unchecked")
        else:
            for path in untracked:
                broken += 1
                print(f"UNTRACKED {path} — present on disk, absent from a fresh checkout")

    for line in warnings:
        print(f"warning: {line}")
    verifiable = checked - stateless
    print(
        f"checked {checked} fixtures: {verifiable} state a verbatim answer and were verified, "
        f"{stateless} state none and cannot be verified this way"
    )
    if not checked:
        print("nothing was checked — wrong tasks root?")
        return 2
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
