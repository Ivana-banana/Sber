"""Contract tests for what the utility harness reads out of an agent log."""

from __future__ import annotations

from pathlib import Path

from scripts.eval.run_suite import (
    PhaseBreakdown,
    SuiteResult,
    TaskResult,
    phase_breakdown,
    write_report,
)
from scripts.eval.summarise import phase_report
from scripts.eval.timing import read_run


READY = (
    "agent ready: workspace=/app (image workdir /app) instruction=42:0123456789ab "
    "budget=600s reserve=120s steps=30 endpoint=configured\n"
)


def phase(step: int, name: str, status: str, duration: float) -> str:
    return (
        f"agent phase: step={step} phase={name} status={status} "
        f"duration_s={duration:.3f}\n"
    )


def killed_log() -> str:
    """A timeout with the E018 shape: completed plans, tiny acts, no verify."""
    return (
        READY
        + phase(0, "inspect", "start", 0.0)
        + phase(0, "inspect", "end", 0.004)
        + phase(1, "plan", "start", 0.0)
        + phase(1, "plan", "end", 72.182)
        + phase(1, "act", "start", 0.0)
        + phase(1, "act", "end", 0.009)
        + phase(2, "plan", "start", 0.0)
        + phase(2, "plan", "end", 17.808)
        + phase(2, "act", "start", 0.0)
        + phase(2, "act", "end", 0.008)
        + phase(3, "plan", "start", 0.0)
    )


def test_phase_breakdown_totals_completed_time_per_phase() -> None:
    """Without per-phase totals a timeout cannot be blamed on generation or on tools."""
    breakdown = phase_breakdown(killed_log())

    assert breakdown.seconds["plan"] == 89.990
    assert breakdown.seconds["act"] == 0.017
    assert breakdown.seconds["inspect"] == 0.004
    assert "verify" not in breakdown.seconds
    assert breakdown.completed == {"inspect": 1, "plan": 2, "act": 2}


def test_phase_breakdown_names_the_phase_that_was_still_running() -> None:
    """The unclosed phase is the evidence of what held the deadline at the kill."""
    assert phase_breakdown(killed_log()).open_phase == "plan@3"


def test_a_finished_run_leaves_no_phase_open() -> None:
    log = killed_log() + phase(3, "plan", "end", 1.5) + phase(3, "verify", "start", 0.0) + phase(
        3, "verify", "end", 2.25
    )

    breakdown = phase_breakdown(log)

    assert breakdown.open_phase == ""
    assert breakdown.seconds["verify"] == 2.25


def test_a_log_without_phase_lines_is_empty_rather_than_an_error() -> None:
    """Logs from before the telemetry existed must stay readable by the harness."""
    breakdown = phase_breakdown(READY + "agent done: solved=False stop=killed\n")

    assert breakdown.seconds == {}
    assert breakdown.open_phase == ""
    assert breakdown.render() == "-"


def test_the_breakdown_renders_seconds_and_call_counts_in_phase_order() -> None:
    assert phase_breakdown(killed_log()).render() == (
        "inspect=0.004/1 plan=89.990/2 act=0.017/2 open=plan@3"
    )


def test_the_table_says_whether_a_run_ever_wrote_anything(tmp_path: Path) -> None:
    """The second gate. E037 kept solved at 2/18 while deliverables fell 14 -> 3."""
    rows = [
        TaskResult(
            task="t1", repeat=1, reward=0, agent_exit=124, wall_s=95.1, tokens=None,
            steps=None, stop_reason="killed", solved_claim="-", workspace="/app",
            failure_mode="timeout", wrote="yes",
        ),
        TaskResult(
            task="t2", repeat=1, reward=0, agent_exit=124, wall_s=95.1, tokens=None,
            steps=None, stop_reason="killed", solved_claim="-", workspace="/app",
            failure_mode="timeout", wrote="no",
        ),
    ]
    suite = SuiteResult(
        label="test", model="stub", base_url="http://127.0.0.1:8000/v1",
        timeout_scale=1.0, started="2026-09-03T00:00:00", rows=rows,
    )

    write_report(tmp_path, suite)

    lines = (tmp_path / "results.tsv").read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    column = header.index("wrote")
    assert [line.split("\t")[column] for line in lines[1:]] == ["yes", "no"]


def test_the_results_table_carries_the_breakdown_for_every_row(tmp_path: Path) -> None:
    """A per-task breakdown that only exists in a log is a manual grep, not a metric."""
    row = TaskResult(
        task="forensics-sharded-exfil", repeat=1, reward=0, agent_exit=124, wall_s=95.1,
        tokens=None, steps=None, stop_reason="killed", solved_claim="-", workspace="/app",
        failure_mode="timeout", phases=phase_breakdown(killed_log()).render(),
    )
    suite = SuiteResult(
        label="test", model="stub", base_url="http://127.0.0.1:8000/v1",
        timeout_scale=1.0, started="2026-09-03T00:00:00", rows=[row],
    )

    write_report(tmp_path, suite)

    header, first = (tmp_path / "results.tsv").read_text(encoding="utf-8").splitlines()[:2]
    assert "phases" in header.split("\t")
    assert first.split("\t")[header.split("\t").index("phases")] == (
        "inspect=0.004/1 plan=89.990/2 act=0.017/2 open=plan@3"
    )


def test_a_rendered_breakdown_parses_back_into_the_same_numbers() -> None:
    """The TSV cell is the only form a summary sees; it has to be lossless."""
    original = phase_breakdown(killed_log())

    assert PhaseBreakdown.parse(original.render()) == original


def test_parsing_a_run_without_telemetry_yields_an_empty_breakdown() -> None:
    assert PhaseBreakdown.parse("-") == PhaseBreakdown()


def row(task: str, wall_s: float, phases: str) -> dict[str, str]:
    return {"task": task, "wall_s": str(wall_s), "phases": phases, "reward": "0", "failure": "timeout"}


def test_the_phase_report_answers_share_cost_and_place_of_death() -> None:
    """Three numbers decide the next ablation; a reader must not compute them by hand."""
    rows = [
        row("t1", 95.1, "inspect=0.004/1 plan=89.990/3 act=0.017/2 open=plan@4"),
        row("t2", 180.0, "inspect=0.002/1 plan=90.000/9 act=0.010/6 verify=1.000/1"),
        row("t3", 100.0, "plan=80.000/4 act=0.020/3 open=act@5"),
        row("t4", 12.0, "-"),
    ]

    assert phase_report(rows) == [
        "phase plan  median 80.0% of wall, min 50.0% (t2), max 94.6% (t1)",
        "phase cost  16 plan calls, mean 16.2 s; dearest t1 at 30.0 s/call",
        "phase kill  act 1, none 1, plan 1; deepest step 5 (t3)",
        "phase gaps  1 row(s) without telemetry",
    ]


def test_a_run_killed_inside_its_first_plan_reports_no_completed_call() -> None:
    """The worst case measured in E018 must not divide by zero."""
    rows = [row("t1", 95.1, "inspect=0.004/1 open=plan@1")]

    assert phase_report(rows)[1] == "phase cost  no plan call completed anywhere"


def test_a_run_without_any_telemetry_adds_no_lines() -> None:
    """E017-era directories must keep summarising exactly as before."""
    assert phase_report([row("t1", 95.1, "-")]) == []


def write_run(root: Path, log: str, trace: str) -> Path:
    """One task's artefacts in the layout `run_suite.py` writes."""
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "t1.r1.log").write_text(log, encoding="utf-8")
    (logs / "t1.r1.trace").write_text(trace, encoding="utf-8")
    return root


def test_timing_charges_a_ladder_demotion_to_the_step_it_happened_in(
    tmp_path: Path,
) -> None:
    """The first call of E033's `fix-redirect-scheme` was answered in the wrong schema.

    The retry shares step 1, so the `write_file` that the proxy records as call 5 ran
    in step 4 and must not be charged with step 5's plan.
    """
    log = (
        READY
        + phase(0, "inspect", "start", 0.0)
        + phase(0, "inspect", "end", 0.003)
        + phase(1, "plan", "start", 0.0)
        + phase(1, "plan", "end", 76.092)
        + phase(1, "act", "start", 0.0)
        + phase(1, "act", "end", 0.0)
        + phase(2, "plan", "start", 0.0)
        + phase(2, "plan", "end", 7.826)
        + phase(2, "act", "start", 0.0)
        + phase(2, "act", "end", 0.0)
        + phase(3, "plan", "start", 0.0)
        + phase(3, "plan", "end", 6.085)
        + phase(4, "plan", "start", 0.0)
        + phase(4, "plan", "end", 17.504)
        + phase(4, "act", "start", 0.0)
        + phase(4, "act", "end", 0.001)
        + phase(5, "plan", "start", 0.0)
        + phase(5, "plan", "end", 18.708)
    )
    trace = (
        '--- call 1 ---\n{"name": "read_range", "arguments": {"path": "/app/a.py"}}\n'
        '--- call 2 ---\n{"tool": "read_range", "path": "/app/a.py"}\n'
        '--- call 3 ---\n{"tool": "read_range", "path": "/app/a.py"}\n'
        '--- call 4 ---\n{"tool": "read_range", "path": "/app/a.py"}\n'
        '--- call 5 ---\n{"tool": "write_file", "path": "/app/a.py"}\n'
        '--- call 6 ---\n{"tool": "finish", "path": "/app/a.py"}\n'
    )

    task = read_run(write_run(tmp_path, log, trace))[0]

    assert task.first_plan_s == 76.092
    assert task.plan_calls == 5
    assert task.aligned
    assert task.first_write_step == 4
    assert round(task.time_to_first_write_s, 3) == 107.511
    assert round(task.naive_time_to_first_write_s, 3) == 126.219


def test_timing_refuses_to_place_a_write_it_could_not_align(tmp_path: Path) -> None:
    """A trace with more tool calls than the log has plans is not guessed at."""
    log = (
        READY
        + phase(1, "plan", "start", 0.0)
        + phase(1, "plan", "end", 10.0)
        + phase(1, "act", "start", 0.0)
        + phase(1, "act", "end", 0.0)
    )
    trace = (
        '--- call 1 ---\n{"tool": "list_dir", "path": "/app"}\n'
        '--- call 2 ---\n{"tool": "write_file", "path": "/app/a.py"}\n'
    )

    task = read_run(write_run(tmp_path, log, trace))[0]

    assert not task.aligned
    assert task.time_to_first_write_s is None


def test_timing_separates_the_first_call_from_the_ones_that_reuse_the_prefix(
    tmp_path: Path,
) -> None:
    """E034 read as a regression until its later calls showed the whole stand slower."""
    log = (
        READY
        + phase(1, "plan", "start", 0.0)
        + phase(1, "plan", "end", 48.7)
        + phase(1, "act", "start", 0.0)
        + phase(1, "act", "end", 0.0)
        + phase(2, "plan", "start", 0.0)
        + phase(2, "plan", "end", 9.1)
        + phase(3, "plan", "start", 0.0)
        + phase(3, "plan", "end", 8.9)
    )
    trace = '--- call 1 ---\n{"tool": "list_dir", "path": "/app"}\n'

    task = read_run(write_run(tmp_path, log, trace))[0]

    assert task.first_plan_s == 48.7
    assert task.later_plan_s == [9.1, 8.9]
    assert task.steady_plan_s == [9.1, 8.9]


def test_timing_stops_the_steady_window_before_the_observation_window_slides(
    tmp_path: Path,
) -> None:
    """From step 9 a call re-prefills the whole context, so it measures the agent too."""
    log = READY + "".join(
        phase(step, "plan", "start", 0.0) + phase(step, "plan", "end", float(step))
        for step in range(1, 11)
    )
    trace = '--- call 1 ---\n{"tool": "list_dir", "path": "/app"}\n'

    task = read_run(write_run(tmp_path, log, trace))[0]

    assert task.steady_plan_s == [2.0, 3.0, 4.0, 5.0, 6.0]
    assert task.later_plan_s[-1] == 10.0
