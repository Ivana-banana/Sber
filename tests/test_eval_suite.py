"""Contract tests for what the utility harness reads out of an agent log."""

from __future__ import annotations

from pathlib import Path

from scripts.eval.run_suite import (
    PhaseBreakdown,
    SuiteResult,
    TaskResult,
    phase_breakdown,
    task_names,
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


def test_a_directory_without_a_task_toml_is_not_a_task(tmp_path):
    """The public snapshot ships `insecure-api-app`, which is a shared app, not a task."""
    (tmp_path / "hello-file").mkdir()
    (tmp_path / "hello-file" / "task.toml").write_text("[task]\n", encoding="utf-8")
    (tmp_path / "insecure-api-app").mkdir()
    (tmp_path / "insecure-api-app" / "sync.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    assert task_names(tmp_path) == ["hello-file"]


def result(**changes):
    values = dict(task='sample', repeat=1, reward=1, agent_exit=0, wall_s=1,
                  tokens=10, steps=1, stop_reason='verified', solved_claim='True',
                  workspace='/app', failure_mode='-')
    values.update(changes)
    return TaskResult(**values)


def test_reports_separate_checker_reward_from_timeout_eligibility(tmp_path):
    import json
    suite = SuiteResult(label='eligibility', model='stub', base_url='local',
                        timeout_scale=1, started='now', expected_tasks=['sample'], repeats=5,
                        rows=[result(repeat=1, agent_exit=124, stop_reason='killed'),
                              result(repeat=2), result(repeat=3, reward=0),
                              result(repeat=4, reward=None), result(repeat=5, agent_exit=2)])
    write_report(tmp_path, suite)
    saved = json.loads((tmp_path / 'suite.json').read_text())
    assert saved['complete'] is True
    assert [r['eligible_success'] for r in saved['rows']] == [False, True, False, False, False]
    assert saved['rows'][0]['reward'] == 1
    assert saved['rows'][0]['timed_out'] is True
    header = (tmp_path / 'results.tsv').read_text().splitlines()[0].split('\t')
    assert header[-3:] == ['agent_exit', 'timed_out', 'eligible_success']
    suite.rows.pop()
    write_report(tmp_path, suite)
    assert json.loads((tmp_path / 'suite.json').read_text())['complete'] is False


def test_existing_output_is_rejected_before_unpacking(tmp_path, monkeypatch):
    import sys
    from scripts.eval.run_suite import main
    preserved = tmp_path / 'unpacked' / 'precious'
    preserved.parent.mkdir()
    preserved.write_bytes(b'old evidence')
    monkeypatch.setattr(sys, 'argv', ['run_suite', '--model', 'stub', '--base-url', 'local',
                                     '--out', str(tmp_path), '--archive', 'missing.zip'])
    assert main() == 2
    assert preserved.read_bytes() == b'old evidence'
    # The rejection must be specifically about existing output, not a later archive error.
    from scripts.eval.run_suite import reserve_output
    import pytest
    with pytest.raises(FileExistsError):
        reserve_output(tmp_path)


def test_write_requests_follow_the_runtime_response_decoder():
    from scripts.eval.run_suite import requested_write
    import json
    action = {'tool': 'write_file', 'path': '/app/report', 'content': 'done'}
    for message in [
        {'content': json.dumps(action)},
        {'content': '```json\n' + json.dumps(action) + '\n```'},
        {'content': json.dumps({'name': 'write_file', 'arguments': {'path': '/app/report', 'content': 'done'}})},
        {'tool_calls': [{'function': {'name': 'emit_action', 'arguments': json.dumps(action)}}]},
        {'tool_calls': {'function': {'name': 'emit_action', 'parameters': action}}},
        {'tool_calls': [{'name': 'emit_action', 'arguments': action}]},
    ]:
        assert requested_write(message), message
    assert not requested_write({'content': json.dumps({'tool': 'read_file', 'path': 'x'})})
    assert not requested_write({'content': 'We should call write_file later.'})
    assert not requested_write({'content': '{"name":"write_file","arguments":null}'})


def test_proxy_exchange_preserves_native_write_for_requested_write_with_truncation():
    import json
    from scripts.eval.counting_proxy import Counters, EXCHANGE_CHARS
    from scripts.eval.run_suite import requested_write

    action = {'tool': 'write_file', 'path': '/app/report', 'content': 'done'}
    payload = json.dumps({'choices': [{'message': {
        'content': 'x' * (EXCHANGE_CHARS + 100),
        'tool_calls': [{'function': {'name': 'emit_action', 'arguments': json.dumps(action)}}],
    }}]}).encode()
    counters = Counters()
    counters.requests = 1

    counters.remember(payload)

    stored = counters.exchanges[0]
    assert len(stored['content']) == EXCHANGE_CHARS
    assert isinstance(stored['tool_calls'], list)
    assert requested_write(stored)


def test_historical_stringified_tool_calls_are_parsed_safely_and_bounded():
    import json
    from scripts.eval.counting_proxy import EXCHANGE_CHARS
    from scripts.eval.run_suite import requested_write

    content_action = {'tool': 'write_file', 'path': '/app/report', 'content': 'done'}
    assert requested_write({'content': json.dumps(content_action), 'tool_calls': ''})
    historical = "[{'function': {'name': 'emit_action', 'arguments': " \
        "'{\"tool\": \"write_file\", \"path\": \"/app/report\", \"content\": \"done\"}'}}]"
    assert requested_write({'tool_calls': historical})
    assert not requested_write({'tool_calls': historical + ' ' * EXCHANGE_CHARS})


def test_verifier_evidence_is_bounded_and_missing_logs_are_explicit(tmp_path, monkeypatch):
    import argparse
    import subprocess
    from scripts.eval import run_suite
    from scripts.eval.run_suite import Runner, TaskSpec
    args = argparse.Namespace(out=str(tmp_path))
    runner = Runner(args, tmp_path)
    spec = TaskSpec('sample', tmp_path, 'task', '/app', False, 10, 10, 10, 1, 512)
    def fake_run(cmd, **kwargs):
        assert cmd[:3] == ['docker', 'exec', 'container']
        # Run the exact extraction command locally against a real temporary log tree.
        script = cmd[-1].replace('/logs/verifier', str(tmp_path / 'source'))
        return subprocess.run(
            cmd[3:-1] + [script], capture_output=True,
            text=kwargs.get('text', True), check=False,
        )
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'verifier.log').write_text('a' * 70000 + 'FINAL ASSERTION')
    monkeypatch.setattr(run_suite, 'run', fake_run)
    runner.save_verifier_logs('container', spec, 1)
    log = tmp_path / 'logs' / 'sample.r1.verifier.log'
    assert log.stat().st_size <= 65536
    assert log.read_text().endswith('FINAL ASSERTION')
    assert 'unavailable' in (tmp_path / 'logs' / 'sample.r1.app.log').read_text()


def test_verify_uses_the_packages_verifier_timeout_and_parses_a_float_reward(tmp_path, monkeypatch):
    import argparse
    import subprocess
    from scripts.eval import run_suite
    from scripts.eval.run_suite import Runner, TaskSpec

    args = argparse.Namespace(out=str(tmp_path))
    runner = Runner(args, tmp_path)
    # A verifier that brings up PostgreSQL is given 240 s by its task.toml; a hardcoded
    # 180 s recorded the slow-but-correct verifier as reward=None. And test.sh may write
    # `1.0`, which int() alone rejected -- a real reward lost to its formatting.
    spec = TaskSpec('sample', tmp_path, 'task', '/app', False, 10, 240.0, 10, 1, 512)
    seen = {}

    def fake_run(cmd, *, timeout=run_suite.DOCKER_STEP_TIMEOUT_S, text=True):
        if cmd[-1].endswith('test.sh'):
            seen['timeout'] = timeout
            return subprocess.CompletedProcess(cmd, 0, '', '')
        if cmd[-1] == run_suite.REWARD_PATH:
            return subprocess.CompletedProcess(cmd, 0, '1.0\n', '')
        return subprocess.CompletedProcess(cmd, 0, '', '')

    monkeypatch.setattr(run_suite, 'run', fake_run)

    reward, note = runner.verify('container', spec)

    assert seen['timeout'] == 240.0, "the verifier gets its own budget, not a fixed 180 s"
    assert reward == 1 and note == "", "a `1.0` reward is a solve, not an unreadable file"


def test_binary_verifier_tail_is_preserved_and_does_not_discard_reward(tmp_path, monkeypatch):
    import argparse
    import subprocess
    from scripts.eval import run_suite
    from scripts.eval.run_suite import Runner, TaskSpec

    args = argparse.Namespace(
        out=str(tmp_path), counter_container='', timeout_scale=1, model='stub',
        base_url='local', api_key='', controller='', tell_agent_budget=False,
        network='none', reuse_images=True,
    )
    runner = Runner(args, tmp_path)
    spec = TaskSpec('sample', tmp_path, 'task', '/app', False, 10, 10, 10, 1, 512)
    tail = b'\x82\xac' + b'a' * 65534

    monkeypatch.setattr(runner, 'build', lambda spec: None)
    monkeypatch.setattr(runner, 'start_container', lambda spec: ('container', ''))
    monkeypatch.setattr(runner, 'install', lambda container, spec: '')
    done = ('agent done: solved=True stop=verified stage=checked deliverable=/app/report '
            'steps=1/30 recovery=0 tokens=15\n')
    proc = subprocess.CompletedProcess([], 0, done, '')
    monkeypatch.setattr(runner, 'exec_agent', lambda container, spec: (proc, 1.0, False))
    monkeypatch.setattr(runner, 'counter_call', lambda path, method: None)
    monkeypatch.setattr(runner, 'verify', lambda container, spec: (1, ''))

    def fake_run(cmd, *, timeout=run_suite.DOCKER_STEP_TIMEOUT_S, text=True):
        if cmd[-2:-1] == ['-c']:
            if text:
                raise UnicodeDecodeError('utf-8', tail, 0, 1, 'split character')
            return subprocess.CompletedProcess(cmd, 0, tail, b'')
        return subprocess.CompletedProcess(cmd, 0, '' if text else b'', '' if text else b'')

    monkeypatch.setattr(run_suite, 'run', fake_run)

    result = runner.one(spec, 1)

    assert result.reward == 1
    assert (tmp_path / 'logs' / 'sample.r1.verifier.log').read_bytes() == tail


def test_verifier_log_extraction_timeout_is_saved_without_raising(tmp_path, monkeypatch):
    import argparse
    import subprocess
    from scripts.eval import run_suite
    from scripts.eval.run_suite import Runner, TaskSpec

    runner = Runner(argparse.Namespace(out=str(tmp_path)), tmp_path)
    spec = TaskSpec('sample', tmp_path, 'task', '/app', False, 10, 10, 10, 1, 512)

    def timed_out(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(run_suite, 'run', timed_out)
    runner.save_verifier_logs('container', spec, 1)

    assert (tmp_path / 'logs' / 'sample.r1.verifier.log').read_bytes() == b'unavailable: extraction timeout'


def test_manifest_and_partial_suite_survive_interruption(tmp_path, monkeypatch):
    import hashlib
    import json
    import sys
    import zipfile
    import pytest
    from scripts.eval import run_suite
    tasks = tmp_path / 'tasks'
    pkg = tasks / 'sample'
    (pkg / 'environment').mkdir(parents=True)
    (pkg / 'environment' / 'Dockerfile').write_text('FROM local\nWORKDIR /app\n')
    (pkg / 'task.toml').write_text('[agent]\ntimeout_sec=10\n')
    (pkg / 'instruction.md').write_text('write a report')
    archive = tmp_path / 'agent.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('run.sh', '#!/bin/sh\nexit 0\n')
    out = tmp_path / 'new'
    monkeypatch.setattr(sys, 'argv', ['suite', '--model', 'stub', '--base-url', 'local',
        '--api-key', 'never-save-this-key', '--out', str(out), '--archive', str(archive),
        '--tasks-root', str(tasks)])
    def interrupted(self, spec, repeat):
        raise KeyboardInterrupt
    monkeypatch.setattr(run_suite.Runner, 'one', interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_suite.main()
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['archive_sha256'] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert manifest['expected_tasks'] == ['sample']
    assert 'never-save-this-key' not in (out / 'manifest.json').read_text()
    assert json.loads((out / 'suite.json').read_text())['complete'] is False


def test_reusing_images_records_exact_image_without_build_or_network(tmp_path, monkeypatch):
    import argparse
    import json
    import subprocess
    from scripts.eval import run_suite
    runner = run_suite.Runner(argparse.Namespace(out=str(tmp_path), reuse_images=True), tmp_path)
    spec = run_suite.TaskSpec('sample', tmp_path, 'task', '/app', False, 10, 10, 10, 1, 512)
    (tmp_path / 'manifest.json').write_text('{"images": {}}')
    def local_inspect(cmd, **kwargs):
        assert cmd[:3] == ['docker', 'image', 'inspect']
        return subprocess.CompletedProcess(cmd, 0, 'sha256:local-image\n', '')
    monkeypatch.setattr(run_suite, 'run', local_inspect)
    assert runner.build(spec) is None
    assert json.loads((tmp_path / 'manifest.json').read_text())['images']['sample'] == 'sha256:local-image'


def test_verifier_runs_where_the_package_scripts_expect_their_tests(tmp_path, monkeypatch):
    """Three of the six public packages call `pytest /tests` by absolute path.

    Copying them anywhere else makes their verifier collect nothing and write reward
    0, so a correct report scores exactly like an empty one and no gain on those
    tasks can ever be observed.
    """
    import argparse
    import subprocess
    from scripts.eval import run_suite
    from scripts.eval.run_suite import Runner, TaskSpec

    runner = Runner(argparse.Namespace(out=str(tmp_path)), tmp_path)
    spec = TaskSpec('sample', tmp_path, 'task', '/app', False, 10, 10, 10, 1, 512)
    commands = []

    def record(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, '1', '')

    monkeypatch.setattr(run_suite, 'run', record)

    runner.install_verifier('container', spec)
    reward, note = runner.verify('container', spec)

    assert [cmd[-1] for cmd in commands if cmd[:2] == ['docker', 'cp']] == ['container:/tests']
    assert ['docker', 'exec', 'container', 'bash', '/tests/test.sh'] in commands
    assert (reward, note) == (1, '')


def test_the_expected_answers_are_absent_while_the_agent_runs(tmp_path, monkeypatch):
    """`tests/` holds expected output -- forensics ships the exact expected report.

    Installed before the agent phase it sits inside the container the agent explores,
    and a run that read it would be scored a solve for copying the answer.
    """
    import argparse
    import json
    import subprocess
    from scripts.eval import run_suite
    from scripts.eval.run_suite import Runner, TaskSpec

    (tmp_path / 'manifest.json').write_text(json.dumps({'images': {}}), encoding='utf-8')
    args = argparse.Namespace(out=str(tmp_path), counter_container='')
    runner = Runner(args, tmp_path)
    spec = TaskSpec('sample', tmp_path, 'task', '/app', False, 10, 10, 10, 1, 512)
    order = []

    def record(cmd, **kwargs):
        if cmd[:2] == ['docker', 'cp']:
            order.append(cmd[-1])
        if kwargs.get('text') is False:
            return subprocess.CompletedProcess(cmd, 0, b'', b'')
        return subprocess.CompletedProcess(cmd, 0, '0', '')

    def agent(container, spec):
        order.append('agent ran')
        return subprocess.CompletedProcess([], 0, '', ''), 1.0, False

    monkeypatch.setattr(run_suite, 'run', record)
    monkeypatch.setattr(runner, 'build', lambda spec: None)
    monkeypatch.setattr(runner, 'start_container', lambda spec: ('container', ''))
    monkeypatch.setattr(runner, 'exec_agent', agent)
    monkeypatch.setattr(runner, 'counter_call', lambda path, method: None)

    runner.one(spec, 1)

    assert order.index('agent ran') < order.index('container:/tests')
