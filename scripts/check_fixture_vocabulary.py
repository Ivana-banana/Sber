#!/usr/bin/env python3
"""Fail when agent code repeats a literal that only our own checkers know.

`check_no_task_hardcode.py` answers "does the agent name a public task?" from a hand
written regex. This answers a different and larger question: does any string the agent
ships repeat a literal taken from a fixture's *checker* -- an answer key, a deliverable
filename, an expected value, a required format?

Why it matters for a playbook line. A prompt that says "report the location as
`file:line`" scores better on every fixture whose `check.py` compares `file:line`, and
worse on a hidden task that asks for something else; the measurement improves while the
agent gets narrower. The honest form of the same line is "repeat the format the task
asks for". The difference is mechanical: one repeats a literal from `tests/`, the other
does not. The vocabulary is built from the fixtures on disk, so a new task package is
covered the day it lands instead of when someone remembers to extend a regex.

What counts as a literal worth flagging: it comes from a task's `tests/` (or the
answer-file path named in its instruction), it is at least MIN_LENGTH characters, and it
is *distinctive* -- it carries a digit, dot, slash, dash, underscore or colon, so plain
English words the prompt legitimately uses ("report", "verify") never match.

Docstrings and comments are exempt, as in the sibling check: they document experiments.
Tests are not scanned at all -- they are supposed to name fixtures.

    python3 scripts/check_fixture_vocabulary.py [--show-vocabulary]

Exit 0 when the agent's shipped strings repeat none of them.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TASK_ROOTS = (ROOT / "local_task" / "tasks", ROOT / "local_task_extra" / "tasks")

MIN_LENGTH = 5
DISTINCTIVE = re.compile(r"[0-9./\-_:]")
"""A literal without one of these is an ordinary word, and the prompt is allowed to use
ordinary words. `attacker_ip` and `/app/incident_report.txt` qualify; `report` does not."""

ALLOWED = frozenset(
    {
        # Contract surface the agent is required to know, named by the task statement
        # rather than by a checker: the workspace root and the entrypoint.
        "/app",
        "run.sh",
        # Encodings, media types and interpreter names appear in a checker because every
        # Python file mentions them, not because they belong to a task. Flagging them
        # would bury the one literal that matters under a hundred that do not.
        "utf-8",
        "utf8",
        "ascii",
        "latin-1",
        "application/json",
        "text/plain",
        "python3",
        "/bin/sh",
        "/bin/bash",
        "/usr/bin/env",
    }
)

STRING_LITERAL = re.compile(r"""['"]([^'"\n]{%d,})['"]""" % MIN_LENGTH)


def distinctive(value: str) -> bool:
    return len(value) >= MIN_LENGTH and bool(DISTINCTIVE.search(value)) and value not in ALLOWED


def vocabulary(roots: tuple[pathlib.Path, ...] = TASK_ROOTS) -> dict[str, str]:
    """literal -> where it came from, over every fixture package on disk."""
    found: dict[str, str] = {}

    def note(value: str, origin: str) -> None:
        value = value.strip()
        if distinctive(value):
            found.setdefault(value, origin)

    for root in roots:
        if not root.is_dir():
            continue
        for task in sorted(p for p in root.iterdir() if (p / "task.toml").is_file()):
            note(task.name, f"{task.name}: package name")
            tests = task / "tests"
            for path in sorted(tests.rglob("*")) if tests.is_dir() else []:
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                origin = f"{task.name}: {path.relative_to(task)}"
                if path.suffix == ".txt":
                    for line in text.splitlines():
                        for part in line.split("=", 1):
                            note(part, origin)
                else:
                    for match in STRING_LITERAL.findall(text):
                        note(match, origin)
            instruction = task / "instruction.md"
            if instruction.is_file():
                for match in re.findall(r"`([^`\n]+)`", instruction.read_text(encoding="utf-8")):
                    # Only the answer artefact, not every backticked word: a path.
                    if "/" in match:
                        note(match, f"{task.name}: instruction.md")
    return found


def docstring_nodes(tree: ast.AST) -> set[int]:
    """Every string that documents rather than ships.

    Two kinds. The leading docstring of a module, class or function, and the attribute
    docstring of PEP 258 -- a bare string right after an assignment, which this codebase
    uses for nearly every constant. Missing the second kind makes the check useless:
    those docstrings are exactly where the experiments that name fixtures are recorded,
    and the noise would drown the one shipped string that matters."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        previous: ast.stmt | None = None
        for index, statement in enumerate(body or []):
            leading = index == 0
            after_assignment = isinstance(previous, (ast.Assign, ast.AnnAssign))
            after_docstring = (
                isinstance(previous, ast.Expr)
                and isinstance(previous.value, ast.Constant)
                and isinstance(previous.value.value, str)
                and id(previous.value) in ids
            )
            if (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
                and (leading or after_assignment or after_docstring)
            ):
                ids.add(id(statement.value))
            previous = statement
    return ids


def scan(vocab: dict[str, str], agent_dir: pathlib.Path | None = None) -> list[str]:
    hits: list[str] = []
    base = agent_dir or (ROOT / "agent")
    for path in sorted(base.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docs = docstring_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docs:
                continue
            for literal, origin in vocab.items():
                if literal in node.value:
                    hits.append(f"{path.name}:{node.lineno}: {literal!r} comes from {origin}")
    return hits


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--show-vocabulary", action="store_true")
    args = parser.parse_args(argv[1:])
    vocab = vocabulary()
    if args.show_vocabulary:
        for literal, origin in sorted(vocab.items()):
            print(f"{literal!r:60} {origin}")
        return 0
    hits = scan(vocab)
    if hits:
        print("\n".join(hits), file=sys.stderr)
        print(f"fixture vocabulary in agent strings: {len(hits)}", file=sys.stderr)
        return 1
    print(f"no fixture vocabulary in agent strings (checked {len(vocab)} literals)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
