"""Deterministic dev/holdout split of the extra task set.

Why this exists. The public six tasks are saturated (36/36), so any further change is
tuned on `local_task_extra`. Tuning and measuring on the same 18 tasks would overfit
that set exactly the way the public set was overfit. The split below is fixed *before*
any extra log is read, stratified by category, and derived from a hash of the task
name, so nobody picks which tasks land where.

Rule: within each category sort the tasks by sha256(name); alternate dev/holdout,
starting side alternating across categories in name order so both halves end up with
nine tasks and `mut-*` splits three/three.

Usage: python3 scripts/eval/extra_split.py [tasks-root]  -> prints a table and a
`--only` argument for each side.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def category(name: str) -> str:
    return name.split("-", 1)[0]


def split(names: list[str]) -> dict[str, str]:
    by_cat: dict[str, list[str]] = {}
    for n in sorted(names):
        by_cat.setdefault(category(n), []).append(n)
    sides: dict[str, str] = {}
    start = 0
    for cat in sorted(by_cat):
        ordered = sorted(by_cat[cat], key=lambda n: hashlib.sha256(n.encode()).hexdigest())
        for i, n in enumerate(ordered):
            sides[n] = ("dev", "holdout")[(i + start) % 2]
        start = (start + len(ordered)) % 2
    return sides


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "local_task_extra" / "tasks"
    names = [p.name for p in root.iterdir() if (p / "task.toml").exists()]
    sides = split(names)
    print(f"{'task':32} side")
    for n in sorted(names):
        print(f"{n:32} {sides[n]}")
    for side in ("dev", "holdout"):
        chosen = [n for n in sorted(names) if sides[n] == side]
        print(f"\n{side} ({len(chosen)}): --only {' '.join(chosen)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
