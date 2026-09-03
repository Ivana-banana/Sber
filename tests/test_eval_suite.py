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
