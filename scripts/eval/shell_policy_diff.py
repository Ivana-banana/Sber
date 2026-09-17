#!/usr/bin/env python3
"""Ask two versions of the code-fix shell policy about the commands a model really wrote.

    python3 scripts/eval/shell_policy_diff.py --runs build/eval-KT7-public build/eval-EXTRA-9a8673c \
        --before /path/to/tree-at-old-commit --after /path/to/tree-at-new-commit

A policy change is easy to argue about and cheap to measure: the traces already hold
every `run_shell` string the model asked for. This pulls them out, asks
`_code_fix_shell_refusal` in each tree, and prints what changed. Commands newly allowed
are the benefit; commands newly refused are the regression, and the point of running it
before a stand measurement rather than after.

Both trees are used as working directories for a subprocess, so they may be git
worktrees at any two commits -- the two versions never share an interpreter.

Caveats worth keeping in mind when reading the output. The verdict depends on the
workspace: a trace from `/app` is replayed against a temporary directory, and `--root`
says which prefix to rewrite (default `/app`). A command is judged in isolation, so a
refusal that the loop would never reach (the step before it already failed) still shows
up here. And the corpus is only as wide as the traces given: it says what the change
does to behaviour already seen, not what it does to behaviour not yet observed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

COMMAND_IN_TRACE = re.compile(r'"tool":\s*"run_shell"\s*,\s*"command":\s*"((?:[^"\\]|\\.)*)"')
UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def unescape(text: str) -> str:
    """Trace files carry JSON inside JSON, so one pass of decoding is never enough.

    A command is serialised by the harness and again by the logger, so `>` can arrive as
    a literal backslash followed by `u003e`. Decoding until the string stops changing
    copes with one, two or three layers without knowing which this run produced."""
    for _ in range(4):
        try:
            decoded = json.loads('"' + text.replace('"', '\\"') + '"')
        except ValueError:
            break
        if decoded == text:
            break
        text = decoded
    return UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


def corpus(runs: list[Path]) -> list[str]:
    commands: set[str] = set()
    for run in runs:
        logs = run / "logs" if (run / "logs").is_dir() else run
        for trace in sorted(logs.glob("*.trace")):
            for raw in COMMAND_IN_TRACE.findall(trace.read_text(errors="replace")):
                commands.add(unescape(raw))
    return sorted(commands)


EVALUATE = """
import json, pathlib, shutil, sys
sys.path.insert(0, ".")
from agent import loop
from agent.state import AgentState, Budget

workspace = pathlib.Path(sys.argv[3])
shutil.rmtree(workspace, ignore_errors=True)
(workspace / "tests").mkdir(parents=True)
(workspace / "tests" / "test_a.py").write_text("def test_a(): pass\\n")
(workspace / "app.py").write_text("x = 1\\n")
state = AgentState(instruction="fix the bug", workspace=workspace, budget=Budget(600.0))

verdicts = {}
for command in json.load(open(sys.argv[1])):
    replayed = command.replace(sys.argv[4], str(workspace))
    try:
        verdicts[command] = bool(loop._code_fix_shell_refusal(replayed, state))
    except Exception as exc:
        verdicts[command] = "raised %r" % (exc,)
json.dump(verdicts, open(sys.argv[2], "w"))
"""


def verdicts(tree: Path, commands_file: Path, root: str, label: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        runner = Path(tmp) / "evaluate.py"
        runner.write_text(EVALUATE)
        out = Path(tmp) / "verdicts.json"
        result = subprocess.run(
            [sys.executable, str(runner), str(commands_file), str(out), f"{tmp}/workspace", root],
            cwd=tree,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SystemExit(f"{label} tree failed:\n{result.stderr[-2000:]}")
        return json.loads(out.read_text())


def one_line(command: str, width: int = 100) -> str:
    return command[:width].replace("\n", " / ")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", nargs="+", required=True, type=Path)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path)
    parser.add_argument("--root", default="/app", help="workspace prefix to rewrite (default /app)")
    parser.add_argument("--show-all", action="store_true", help="print every command, not only the changes")
    args = parser.parse_args(argv[1:])

    commands = corpus(args.runs)
    if not commands:
        print("no run_shell commands in those runs (was AGENT_TRACE_TOOLS set?)", file=sys.stderr)
        return 1
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(commands, handle)
        commands_file = Path(handle.name)

    before = verdicts(args.before, commands_file, args.root, "before")
    after = verdicts(args.after, commands_file, args.root, "after")

    allowed = [c for c in commands if before[c] is True and after[c] is False]
    refused = [c for c in commands if before[c] is False and after[c] is True]
    raised = [c for c in commands if isinstance(before[c], str) or isinstance(after[c], str)]

    print(f"{len(commands)} unique commands from {len(args.runs)} run(s)")
    print(f"  refused before : {sum(1 for v in before.values() if v is True)}")
    print(f"  refused after  : {sum(1 for v in after.values() if v is True)}")
    print(f"\nnewly allowed ({len(allowed)}):")
    for command in allowed:
        print("   +", one_line(command))
    print(f"\nnewly refused ({len(refused)}):")
    for command in refused:
        print("   -", one_line(command))
    if raised:
        print(f"\nthe policy raised on ({len(raised)}):")
        for command in raised:
            print("   !", one_line(command), "|", before[command], "->", after[command])
    if args.show_all:
        print("\nevery command:")
        for command in commands:
            mark = {True: "refused", False: "allowed"}
            print(f"   {str(mark.get(before[command], before[command])):8} -> {str(mark.get(after[command], after[command])):8} {one_line(command)}")
    return 1 if refused or raised else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
