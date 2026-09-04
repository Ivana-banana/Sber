"""Turn one or more suite runs into the numbers an experiment card needs.

Development tool. Reads `results.tsv` files written by `run_suite.py`.

    python3 scripts/eval/summarise.py build/eval-E011 build/eval-E012

With two or more runs it prints the per-task comparison as well, because the rule in
CLAUDE.md is that an optimisation is accepted only when the solved count did not
regress -- and that is a per-task question, not a total. A change that solves one new
task and loses a different one shows up as a flat total and must not.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval.run_suite import PhaseBreakdown  # noqa: E402

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


def phase_report(rows: list[dict[str, str]]) -> list[str]:
    """Aggregate the per-task `phases` column into the three timeout questions.

    E018 answered them for one fixture by hand. Across eighteen tasks the same
    arithmetic is the difference between "the model is slow" and "the agent takes
    too many steps", and those two have opposite fixes.
    """
    parsed = [(item, PhaseBreakdown.parse(item.get("phases", "-"))) for item in rows]
    timed = [(item, phases) for item, phases in parsed if phases.seconds and float(item["wall_s"]) > 0]
    if not timed:
        return []

    shares = sorted(
        (phases.seconds.get("plan", 0.0) / float(item["wall_s"]) * 100, item["task"])
        for item, phases in timed
    )
    lines = [
        f"phase plan  median {statistics.median(share for share, _ in shares):.1f}% of wall, "
        f"min {shares[0][0]:.1f}% ({shares[0][1]}), max {shares[-1][0]:.1f}% ({shares[-1][1]})"
    ]

    calls = sum(phases.completed.get("plan", 0) for _, phases in timed)
    seconds = sum(phases.seconds.get("plan", 0.0) for _, phases in timed)
    if calls:
        dearest = max(
            (phases.seconds["plan"] / phases.completed["plan"], item["task"])
            for item, phases in timed
            if phases.completed.get("plan")
        )
        lines.append(
            f"phase cost  {calls} plan calls, mean {seconds / calls:.1f} s; "
            f"dearest {dearest[1]} at {dearest[0]:.1f} s/call"
        )
    else:
        lines.append("phase cost  no plan call completed anywhere")

    killed: dict[str, int] = {}
    for _, phases in timed:
        killed[phases.open_phase.partition("@")[0] or "none"] = (
            killed.get(phases.open_phase.partition("@")[0] or "none", 0) + 1
        )
    deepest = max(
        (
            (int(phases.open_phase.partition("@")[2]), item["task"])
            for item, phases in timed
            if phases.open_phase
        ),
        default=(0, "-"),
    )
    lines.append(
        "phase kill  "
        + ", ".join(f"{name} {count}" for name, count in sorted(killed.items()))
        + f"; deepest step {deepest[0]} ({deepest[1]})"
    )

    missing = len(rows) - len(timed)
    if missing:
        lines.append(f"phase gaps  {missing} row(s) without telemetry")
    return lines


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
    wrote = [row for row in rows if row.get("wrote") == "yes"]
    if any(row.get("wrote", "-") != "-" for row in rows):
        # Second gate, and E037 is why it exists. That run held solved at 2/18 while
        # the tasks leaving any deliverable fell from 14 of 18 to 3 -- the two solved
        # ones are short and survived a collapse that took eleven others.
        print(f"wrote       {len(wrote)}/{len(rows)}")
    print(f"tokens      {tokens}")
    print(f"wall        {wall:.0f}s ({wall / 60:.0f} min)")

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["failure"]] = counts.get(row["failure"], 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: FAILURE_ORDER.index(kv[0]) if kv[0] in FAILURE_ORDER else 99)
    print("failures    " + ", ".join(f"{name} {count}" for name, count in ordered))

    for line in phase_report(rows):
        print(line)

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
