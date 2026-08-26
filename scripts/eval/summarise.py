"""Turn one or more suite runs into the numbers an experiment card needs.

Development tool. Reads `results.tsv` files written by `run_suite.py`.

    python3 scripts/eval/summarise.py build/eval-E009 build/eval-E010

With two or more runs it prints the per-task comparison as well, because the rule in
CLAUDE.md is that an optimisation is accepted only when the solved count did not
regress -- and that is a per-task question, not a total. A change that solves one new
task and loses a different one shows up as a flat total and must not.
"""

from __future__ import annotations

import sys
from pathlib import Path

FAILURE_ORDER = (
    "startup", "packaging", "endpoint", "tool", "reasoning", "verification",
    "timeout", "unknown", "-",
)


def read(directory: Path) -> tuple[str, list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    lines = (directory / "results.tsv").read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    for line in lines[1:]:
        if line.strip():
            rows.append(dict(zip(header, line.split("\t"), strict=False)))
    return directory.name, rows


def number(value: str) -> int:
    return 0 if value in {"?", "", "-"} else int(float(value))


def summarise(label: str, rows: list[dict[str, str]]) -> None:
    solved = [row for row in rows if row["reward"] == "1"]
    # Prefer the endpoint's own count: the agent cannot report its total from a run
    # that was killed, and those are exactly the expensive ones.
    tokens = sum(
        number(row.get("ep_tokens", "?")) or number(row.get("tokens", "?")) for row in rows
    )
    wall = sum(float(row["wall_s"]) for row in rows)
    print(f"\n=== {label} ===")
    print(f"solved      {len(solved)}/{len(rows)}   {' '.join(r['task'] for r in solved) or '-'}")
    print(f"tokens      {tokens}")
    print(f"wall        {wall:.0f}s ({wall / 60:.0f} min)")

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["failure"]] = counts.get(row["failure"], 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: FAILURE_ORDER.index(kv[0]) if kv[0] in FAILURE_ORDER else 99)
    print("failures    " + ", ".join(f"{name} {count}" for name, count in ordered))

    claimed = [row for row in rows if row.get("claim") == "True" and row["reward"] != "1"]
    if claimed:
        print(f"false wins  {len(claimed)}: {' '.join(r['task'] for r in claimed)}")


def compare(runs: list[tuple[str, list[dict[str, str]]]]) -> None:
    tasks = sorted({row["task"] for _, rows in runs for row in rows})
    width = max(len(task) for task in tasks)
    print("\n=== per task ===")
    print(f"{'task':<{width}}  " + "  ".join(f"{label:>14}" for label, _ in runs))
    for task in tasks:
        cells = []
        for _, rows in runs:
            row = next((r for r in rows if r["task"] == task), None)
            cells.append("-" if row is None else f"{row['reward']}/{row['failure'][:10]}")
        print(f"{task:<{width}}  " + "  ".join(f"{cell:>14}" for cell in cells))

    first, last = runs[0][1], runs[-1][1]
    gained = [
        row["task"] for row in last
        if row["reward"] == "1"
        and any(r["task"] == row["task"] and r["reward"] != "1" for r in first)
    ]
    lost = [
        row["task"] for row in first
        if row["reward"] == "1"
        and any(r["task"] == row["task"] and r["reward"] != "1" for r in last)
    ]
    print(f"\ngained {len(gained)}: {' '.join(gained) or '-'}")
    print(f"lost   {len(lost)}: {' '.join(lost) or '-'}")
    if lost:
        print("a regression on any single task blocks the change, whatever the total says")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    runs = [read(Path(argument)) for argument in sys.argv[1:]]
    for label, rows in runs:
        summarise(label, rows)
    if len(runs) > 1:
        compare(runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
