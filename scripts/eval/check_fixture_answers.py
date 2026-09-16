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

Known blind spots, so that a clean run is not read as more than it is: an empty directory
is not tracked by git and will not survive a checkout either; a file staged but never
committed counts as tracked; symbolic links are followed; and a value that has to be
computed (a sum, a hash) is reported as unreachable, because the guard cannot tell it
apart from a lost one.

Usage: python3 scripts/eval/check_fixture_answers.py [tasks-root ...]
Exit 1 if an answer is unreachable, a fixture file is untracked, or the guard was blind
somewhere; 2 if nothing was checked at all; 0 otherwise.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ROOTS = ("local_task", "local_task_extra/tasks")
ANSWER_NAMES = frozenset({"expected", "exp", "want", "answer", "golden", "gold"})
"""Exact names, not substrings. A substring test reads `export`, `unexpected` and
`experiment` as answer dicts — `unexpected_payloads = {...}` in a code-fix checker would
turn a healthy fixture red — and still misses `gold` while matching `golden`."""

# Two characters collide with everything: `ok`, `no`, a one-letter verdict. Reporting
# them as reachable would be luck, not evidence, so they are left out of check 1.
MIN_LENGTH = 3
WORD = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
# Characters that continue a token when what follows them is alphanumeric: `deploy-key`
# does not prove `deploy`, and `admin.example.com` does not prove `admin`, but a value
# at the end of a sentence (`… from deploy.`) still counts.
JOINERS = set(".-/:")


class Warned(list):
    """Problems that must be visible.

    A line prefixed `BLIND:` means the guard could not look at something it was supposed
    to look at — an unreadable evidence file, a checker it could not parse, a tasks root
    git would not answer for. Those decide the exit code: a guard that cannot see must
    not report clean. Everything else is advisory.
    """

    def blind(self) -> list[str]:
        return [line for line in self if line.startswith("BLIND:")]


def read_text(path: Path, warnings: Warned) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        warnings.append(f"BLIND: unreadable, treated as empty: {path} ({exc.strerror})")
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
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
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
        elif isinstance(item, bool):
            continue  # `true` is a verdict, not a value to find in the evidence
        elif isinstance(item, (int, float)):
            answers.add(str(item))  # `{"exfil_bytes": 7340032}` is an answer too
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
        warnings.append(f"BLIND: unparsable checker, no answers taken from it: {where} ({exc.msg})")
        return set()

    named: set[int] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if not targets or not isinstance(node.value, ast.Dict):
            continue
        if any(isinstance(t, ast.Name) and t.id.lower() in ANSWER_NAMES for t in targets):
            named.add(id(node.value))

    answers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict) or id(node) not in named:
            continue
        # Value by value, not all-or-nothing: a single non-string entry
        # (`{'ip': '198.51.100.44', 'attempts': 41}`) used to switch the whole fixture
        # off, and the report then counted it as "states no verbatim answer".
        for item in node.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                answers.add(item.value.strip())
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
    for short in sorted(a for a in answers if len(a) < MIN_LENGTH):
        warnings.append(f"answer too short to look for, not checked: {task.name} {short!r}")
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
        end = start + len(value)
        before = haystack[start - 1] if start else ""
        after = haystack[end : end + 1]
        beyond_before = haystack[start - 2] if start > 1 else ""
        beyond_after = haystack[end + 1 : end + 2]
        cut_left = before in WORD or (before in JOINERS and beyond_before in WORD)
        cut_right = after in WORD or (after in JOINERS and beyond_after in WORD)
        if not cut_left and not cut_right:
            return True
        start = haystack.find(value, start + 1)
    return False


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
    # An empty listing is not "git cannot answer": it means git tracks nothing here, and
    # a tasks root whose every file is absent from a fresh checkout is the incident in
    # full. It used to be downgraded to a warning and a zero exit code.
    tracked = {
        (root / name).resolve()
        for name in listed.stdout.decode("utf-8", "replace").split("\0")
        if name
    }
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
    unknown = [a for a in argv[1:] if a.startswith("--")]
    if unknown:
        print(f"unknown option {unknown[0]!r}; usage: check_fixture_answers.py [tasks-root ...]")
        return 2
    roots = [Path(a) for a in argv[1:]] or [ROOT / r for r in DEFAULT_ROOTS]

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
                    print(f"    {value!r} appears in no file under environment/")

        untracked = untracked_files(root)
        if untracked is None:
            # Not a blind spot but an inapplicable check: a fixture tree copied outside
            # version control has no tracking to verify. The case that *is* a finding —
            # git answering with an empty list — is handled inside `untracked_files`.
            warnings.append(f"not a git repository, tracking unchecked: {root}")
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
    blind = warnings.blind()
    if blind:
        print(f"{len(blind)} blind spots above, so this run does not certify anything")
    return 1 if broken or blind else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
