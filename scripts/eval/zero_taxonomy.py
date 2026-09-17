"""List every zero of a suite run with the evidence a taxonomy row needs.

Development tool. Reads a `run_suite.py` output directory: `results.tsv`, the episode
`.log` (needs `AGENT_TRACE_TOOLS=1` at run time for the `agent tool:` lines), `.trace`
(the model's tool calls per LLM call), `.post` (workspace listing and the claimed
deliverable after a failure) and `.verifier.log`.

    python3 scripts/eval/zero_taxonomy.py build/eval-EXTRA-x [--only task ...] [--verbose]

`--only` matters for the dev/holdout discipline: run it with the dev tasks only and the
holdout logs are never opened. Because that discipline should not rest on remembering a
flag, a run whose tasks all belong to the extra set is narrowed to the dev side by
default; `--all` opens the holdout logs and says so out loud. The class column is a
*hint* from mechanical signals (stop reason, refusals, self-verified claim); the final
class is assigned by reading.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extra_split import split as extra_sides  # noqa: E402

REFUSED_RE = re.compile(r"^agent tool: step=(?P<step>[\d.]+) (?P<tool>\S+) ok=False (?P<text>.*)$")
TOOL_RE = re.compile(r"^agent tool: step=(?P<step>[\d.]+) (?P<tool>\S+) ok=(?P<ok>\w+) (?P<text>.*)$")


EXTRA_TASKS_ROOT = Path(__file__).resolve().parents[2] / "local_task_extra" / "tasks"


def dev_side_default(task_names: set[str]) -> set[str] | None:
    """The dev half of the split, when this run is an extra-set run; otherwise None.

    Opening a holdout log is not undone by deciding to ignore what it said, so the safe
    default belongs in the tool rather than in the operator's memory. A run that is not
    entirely extra tasks (the public six, a mixed run) has no holdout to protect and is
    left alone.
    """
    if not EXTRA_TASKS_ROOT.is_dir():
        return None
    extra = {p.name for p in EXTRA_TASKS_ROOT.iterdir() if (p / "task.toml").is_file()}
    if not task_names or not task_names <= extra:
        return None
    sides = extra_sides(sorted(extra))
    return {name for name in task_names if sides.get(name) == "dev"}


def read_rows(run: Path) -> list[dict[str, str]]:
    with (run / "results.tsv").open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def episode_files(run: Path, task: str, repeat: str) -> dict[str, Path]:
    base = run / "logs" / f"{task}.r{repeat}"
    return {kind: Path(f"{base}.{kind}") for kind in ("log", "trace", "post", "verifier.log", "pytest_output.txt")}


def tool_lines(log: Path) -> list[tuple[str, str, bool, str]]:
    out = []
    if not log.exists():
        return out
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = TOOL_RE.match(line)
        if m:
            out.append((m["step"], m["tool"], m["ok"] == "True", m["text"]))
    return out


def tail_nonempty(path: Path, n: int = 3, width: int = 200) -> list[str]:
    if not path.exists():
        return ["<missing>"]
    lines = [l.strip() for l in path.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    return [l[:width] for l in lines[-n:]]


def hint(row: dict[str, str], tools: list[tuple[str, str, bool, str]]) -> str:
    refusals = [t for t in tools if not t[2] and "refused" in t[3]]
    stop = row.get("stop", "")
    if row.get("timed_out") == "True" and row.get("steps", "0") in {"0", ""}:
        return "startup"
    if row.get("agent_exit") in {"", "None"}:
        return "startup"
    if stop == "verified":
        return "false-verified"
    if refusals and stop in {"steps", "stuck", "blocked"}:
        return "tool-refused?"
    if stop in {"budget"} or row.get("timed_out") == "True":
        return "timeout/budget"
    if stop == "steps":
        return "steps"
    if stop in {"llm-unusable", "crash"}:
        return "tool-broken"
    return "reasoning-wrong-answer?"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run")
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument(
        "--all",
        action="store_true",
        help="open every task's logs, holdout included (an extra-set run defaults to dev)",
    )
    parser.add_argument("--verbose", action="store_true", help="print every tool line of each zero")
    args = parser.parse_args()
    run = Path(args.run)
    rows = read_rows(run)
    zeros = [r for r in rows if r.get("reward", "0") not in {"1", "1.0"}]
    scope_note = ""
    if args.only:
        zeros = [r for r in zeros if r["task"] in set(args.only)]
    elif args.all:
        scope_note = " (--all: holdout logs opened)"
    else:
        dev = dev_side_default({r["task"] for r in rows})
        if dev is not None:
            hidden = sorted({r["task"] for r in zeros} - dev)
            zeros = [r for r in zeros if r["task"] in dev]
            if hidden:
                scope_note = f" (dev side only; holdout left unopened: {', '.join(hidden)})"
    print(f"{run.name}: {len(rows)} rows, {len(zeros)} zeros listed{scope_note}\n")
    print("| задача | повтор | stop | шаги | wall | claim | deliverable | отказы | verifier | класс (hint) |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    details = []
    for r in zeros:
        files = episode_files(run, r["task"], r["repeat"])
        tools = tool_lines(files["log"])
        refusals = [t for t in tools if not t[2] and "refused" in t[3]]
        deliverable = "-"
        log_text = files["log"].read_text(encoding="utf-8", errors="replace") if files["log"].exists() else ""
        m = re.search(r"deliverable=(\S+)", log_text)
        if m:
            deliverable = m.group(1)
        verifier = " / ".join(tail_nonempty(files["verifier.log"], 1, 90))
        first_refusal = refusals[0][3][:90].replace("|", "\\|") if refusals else ""
        print(
            f"| {r['task']} | r{r['repeat']} | {r.get('stop','')} | {r.get('steps','')} | {r.get('wall_s','')} | "
            f"{r.get('claim','')} | {deliverable} | {len(refusals)} {first_refusal} | {verifier.replace('|', '\\|')} | {hint(r, tools)} |"
        )
        details.append((r, files, tools, refusals))
    if args.verbose:
        for r, files, tools, refusals in details:
            print(f"\n=== {r['task']} r{r['repeat']} stop={r.get('stop')} failure={r.get('failure')} note={r.get('note','')[:200]}")
            for step, tool, ok, text in tools:
                mark = "  " if ok else "!!"
                print(f"{mark} {step:>6} {tool:14} {text[:150]}")
            print("-- verifier tail:")
            for line in tail_nonempty(files["verifier.log"], 6):
                print("   " + line)
            if files["post"].exists():
                print("-- post-mortem (claimed deliverable section):")
                post = files["post"].read_text(encoding="utf-8", errors="replace")
                idx = post.find("# claimed deliverable")
                print("   " + (post[idx: idx + 1200] if idx >= 0 else post[:600]).replace("\n", "\n   "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
