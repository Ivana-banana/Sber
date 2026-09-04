"""Continuous timing metrics for one or more suite runs.

Development tool. Reads the `logs/*.log` and `logs/*.trace` files written by
`run_suite.py`.

    python3 scripts/eval/timing.py build/eval-E033-repeat build/eval-E034-prompt-order

`summarise.py` answers "did solved regress". This answers "did the mechanism move",
which after E033 is the only kind of question a single run can answer at all: the
spread of `solved` on unchanged code is three tasks, while the timing of a phase is
an average over dozens of calls and barely moves on noise.

Three numbers matter here.

*First call.* The first `plan` of a task pays prefill for the whole system prompt.
In E033 it was 33% of all planning time for a median reply of 84 characters, so it
is nearly pure prefill and it is what prompt-order changes are supposed to move.

*Plan calls and seconds.* A factor that makes each call cheaper but buys more calls
has not made the task faster; both have to be read together.

*Time to first write.* Reward comes from a file on disk, and in E033 the median task
spent 126 of its 180 seconds before writing one. Logs do not name the tool a step
ran, so the step of the first `write_file` is recovered by walking the trace: the
proxy records one exchange per request, and a request whose reply carries no `tool`
key is a ladder demotion or retry that shares its step with the next one. When that
reconstruction does not land on the number of `plan` steps the log shows, the task is
reported as unaligned rather than guessed at.
"""

from __future__ import annotations

import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

PHASE_LINE = re.compile(
    r"agent phase: step=(\d+) phase=(\w+) status=end duration_s=([\d.]+)"
)
CALL_BLOCK = re.compile(r"^--- call (\d+) ---$", re.MULTILINE)
TOOL_NAME = re.compile(r'"tool"\s*:\s*"(\w+)"')


@dataclass
class Task:
    name: str
    first_plan_s: float | None = None
    plan_calls: int = 0
    plan_s: float = 0.0
    steps: list[tuple[int, str, float]] = field(default_factory=list)
    first_write_step: int | None = None
    aligned: bool = True

    naive_write_step: int | None = None

    def _through(self, step: int | None) -> float | None:
        if step is None:
            return None
        return sum(d for at, _, d in self.steps if at <= step)

    @property
    def time_to_first_write_s(self) -> float | None:
        """Wall time from task start through the step that wrote a file.

        Every phase of every earlier step counts, not just planning: the question is
        how much of the task's 180 seconds passed before anything existed on disk.
        """
        return None if not self.aligned else self._through(self.first_write_step)

    @property
    def naive_time_to_first_write_s(self) -> float | None:
        """The same number under `call N == step N`, which ignores retries.

        E034's prediction was registered against this variant before the run, so it
        is kept to judge that prediction on its own terms. It reads late: a ladder
        demotion at step 1 shifts every later step by one and charges the write with
        a plan it never waited for.
        """
        return self._through(self.naive_write_step)


def read_log(path: Path) -> Task:
    task = Task(name=path.name.split(".r")[0])
    for step, phase, duration in PHASE_LINE.findall(path.read_text(encoding="utf-8")):
        task.steps.append((int(step), phase, float(duration)))
        if phase == "plan":
            task.plan_calls += 1
            task.plan_s += float(duration)
            if task.first_plan_s is None:
                task.first_plan_s = float(duration)
    return task


def locate_first_write(task: Task, trace: Path) -> None:
    """Map trace calls onto plan steps and record where the first `write_file` fell."""
    if not trace.exists():
        task.aligned = False
        return
    text = trace.read_text(encoding="utf-8")
    bounds = [m.start() for m in CALL_BLOCK.finditer(text)] + [len(text)]
    step = 0
    for index in range(len(bounds) - 1):
        body = text[bounds[index] : bounds[index + 1]]
        found = TOOL_NAME.search(body)
        if not found:
            continue  # a retry or ladder demotion: it shares the next call's step
        step += 1
        if found.group(1) == "write_file" and task.first_write_step is None:
            task.first_write_step = step
            task.naive_write_step = index + 1
    task.aligned = step == task.plan_calls


def read_run(directory: Path) -> list[Task]:
    tasks = []
    for log in sorted((directory / "logs").glob("*.log")):
        task = read_log(log)
        locate_first_write(task, log.with_suffix(".trace"))
        tasks.append(task)
    return tasks


def stat(values: list[float]) -> str:
    if not values:
        return "n=0"
    return (
        f"n={len(values)} median={statistics.median(values):.1f} "
        f"mean={statistics.fmean(values):.1f} sum={sum(values):.0f}"
    )


def report(directory: Path, tasks: list[Task]) -> None:
    first = [t.first_plan_s for t in tasks if t.first_plan_s is not None]
    writes = [t.time_to_first_write_s for t in tasks if t.time_to_first_write_s is not None]
    unaligned = [t.name for t in tasks if not t.aligned]
    wrote = [t for t in tasks if t.first_write_step is not None]

    print(f"\n== {directory.name} ({len(tasks)} tasks) ==")
    print(f"first plan call s : {stat(first)}")
    print(f"plan calls        : {sum(t.plan_calls for t in tasks)}")
    print(f"plan seconds      : {sum(t.plan_s for t in tasks):.0f}")
    print(f"tasks that wrote  : {len(wrote)}/{len(tasks)}")
    print(f"time to 1st write : {stat([v for v in writes if v is not None])}")
    naive = [t.naive_time_to_first_write_s for t in tasks]
    print(f"  same, call==step : {stat([v for v in naive if v is not None])}")
    if unaligned:
        print(f"unaligned traces  : {len(unaligned)} ({', '.join(unaligned)})")


def compare(runs: list[tuple[Path, list[Task]]]) -> None:
    base_dir, base = runs[0]
    print(f"\n== per task first plan call, seconds (control {base_dir.name}) ==")
    names = sorted({t.name for _, tasks in runs for t in tasks})
    width = max(len(n) for n in names)
    heads = "".join(f"{d.name[:14]:>16}" for d, _ in runs)
    print(f"{'task':<{width}}{heads}{'delta':>10}")
    for name in names:
        cells = []
        for _, tasks in runs:
            found = next((t for t in tasks if t.name == name), None)
            cells.append(found.first_plan_s if found and found.first_plan_s else None)
        row = "".join(f"{c:>16.1f}" if c is not None else f"{'-':>16}" for c in cells)
        delta = (
            f"{cells[-1] - cells[0]:>+10.1f}"
            if cells[0] is not None and cells[-1] is not None
            else f"{'-':>10}"
        )
        print(f"{name:<{width}}{row}{delta}")
    del base


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    runs = [(Path(a), read_run(Path(a))) for a in argv]
    for directory, tasks in runs:
        report(directory, tasks)
    if len(runs) > 1:
        compare(runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
