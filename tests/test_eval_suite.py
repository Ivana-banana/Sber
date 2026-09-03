"""Contract tests for what the utility harness reads out of an agent log."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.eval.run_suite import (
    PhaseBreakdown,
    SuiteResult,
    TaskResult,
    phase_breakdown,
    write_report,
)
from scripts.eval.summarise import phase_report


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


def test_parsing_skips_unowned_and_malformed_legacy_tokens() -> None:
    rendered = "legacy=1/2 legacy=value inspect=0.004/1 plan=bad/2 open=plan@3"

    assert PhaseBreakdown.parse(rendered) == PhaseBreakdown(
        seconds={"inspect": 0.004}, completed={"inspect": 1}, open_phase="plan@3"
    )


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


def test_a_corrupt_open_step_does_not_crash_the_phase_report() -> None:
    lines = phase_report([row("t1", 1.0, "plan=1.000/1 open=plan@unknown")])

    assert lines[-1] == "phase kill  plan 1; deepest step 0 (-)"


def test_e017_table_remains_compatible_with_direct_summary_script(tmp_path: Path) -> None:
    """The old committed table must stay readable through the real CLI entrypoint."""
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "results.tsv").write_bytes(
        (root / "docs/research/live-runs/E017-results.tsv").read_bytes()
    )

    result = subprocess.run(
        [sys.executable, str(root / "scripts/eval/summarise.py"), str(tmp_path)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "phase " not in result.stdout
    assert "solved      4/18" in result.stdout
