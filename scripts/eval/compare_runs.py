#!/usr/bin/env python3
"""Compare two `run_suite.py` output directories the way the tie-break reads them.

    python3 scripts/eval/compare_runs.py build/eval-A build/eval-B [--exclude task ...]

Prints solved per task for both sides, then the totals in tie-break order --
`solved_count`, then tokens, then wall time -- and, for the extra set, the dev and
holdout halves separately. Tasks named with `--exclude` are dropped from the totals and
shown with a marker; use it whenever a fixture changed between the runs, because a task
that was unsolvable on one side and solvable on the other is not a comparison.

Two habits this exists to enforce. A total is reported next to the per-task rows that
produced it, so a difference that lives in one task cannot hide inside a percentage --
that is how "+12% tokens" turned out to be one task at +50%. And a difference in solved
comes with the count of episodes behind it, because at three repeats a one-episode gap
is noise and saying so in the same line is cheaper than arguing later.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))


def sides() -> dict[str, str]:
    """task -> dev/holdout, or an empty mapping when the extra set is not on disk."""
    try:
        from extra_split import split
    except ImportError:
        return {}
    root = ROOT / "local_task_extra" / "tasks"
    if not root.is_dir():
        return {}
    return split([p.name for p in root.iterdir() if (p / "task.toml").is_file()])


def load(run: Path) -> dict[str, list[int | float]]:
    """task -> [solved, tokens, wall_s, episodes]."""
    totals: dict[str, list[int | float]] = defaultdict(lambda: [0, 0, 0.0, 0])
    with (run / "results.tsv").open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            entry = totals[row["task"]]
            entry[0] += int(row["reward"])
            entry[1] += int(row["tokens"])
            entry[2] += float(row["wall_s"])
            entry[3] += 1
    return dict(totals)


def per_episode(entry: list[int | float]) -> tuple[float, float]:
    episodes = entry[3] or 1
    return entry[1] / episodes, entry[2] / episodes


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("a", help="baseline run directory")
    parser.add_argument("b", help="candidate run directory")
    parser.add_argument("--exclude", nargs="*", default=[], help="tasks to leave out of the totals")
    args = parser.parse_args(argv[1:])

    left, right = load(Path(args.a)), load(Path(args.b))
    excluded = set(args.exclude)
    side = sides()

    print(f"{'task':30} {'side':8} {'A solved':>9} {'B solved':>9} {'A tok/ep':>9} {'B tok/ep':>9} {'A s/ep':>7} {'B s/ep':>7}")
    totals = {"a": [0, 0, 0.0, 0], "b": [0, 0, 0.0, 0]}
    halves: dict[str, dict[str, list[int]]] = {"dev": {"a": [0, 0], "b": [0, 0]}, "holdout": {"a": [0, 0], "b": [0, 0]}}
    for task in sorted(set(left) | set(right)):
        la = left.get(task, [0, 0, 0.0, 0])
        lb = right.get(task, [0, 0, 0.0, 0])
        atok, asec = per_episode(la)
        btok, bsec = per_episode(lb)
        mark = "  EXCLUDED" if task in excluded else ""
        print(
            f"{task:30} {side.get(task, '-'):8} {la[0]:>4}/{la[3]:<4} {lb[0]:>4}/{lb[3]:<4} "
            f"{atok:>9.0f} {btok:>9.0f} {asec:>7.0f} {bsec:>7.0f}{mark}"
        )
        if task in excluded:
            continue
        for key, entry in (("a", la), ("b", lb)):
            for i in range(4):
                totals[key][i] += entry[i]
        half = halves.get(side.get(task, ""))
        if half is not None:
            half["a"][0] += la[0]
            half["a"][1] += la[3]
            half["b"][0] += lb[0]
            half["b"][1] += lb[3]

    a, b = totals["a"], totals["b"]
    print()
    print(f"solved   A {a[0]}/{a[3]}   B {b[0]}/{b[3]}   difference {b[0] - a[0]:+d} episodes")
    for name, half in halves.items():
        if half["a"][1] or half["b"][1]:
            print(f"  {name:8} A {half['a'][0]}/{half['a'][1]}   B {half['b'][0]}/{half['b'][1]}")
    if a[1] and b[1]:
        print(f"tokens   A {a[1]:,}   B {b[1]:,}   ({b[1] / a[1] - 1:+.1%})")
    if a[2] and b[2]:
        print(f"wall     A {a[2]:.0f}s   B {b[2]:.0f}s   ({b[2] / a[2] - 1:+.1%})")
    if excluded:
        print(f"excluded from the totals: {', '.join(sorted(excluded))}")
    if abs(b[0] - a[0]) <= 2 and a[3] <= 60:
        print("note: a gap of one or two episodes at this many repeats is not a result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
