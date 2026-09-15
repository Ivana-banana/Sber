"""What the proxy says is in flight, and what the harness does with that answer.

The endpoint serves one request at a time and never cancels one. A call the agent
walked away from -- its own clamp, the harness kill, a budget stop -- keeps generating
with nobody listening, and the next episode's first call queues behind it. On the
RTX 4080 stand (F4-30b-paired) that cost two cells outright and left 31 requests per
arm over 60 s for five to seven seconds of work.

So two things have to hold. The proxy must keep an honest count of open upstream
requests including the ones whose client is gone, and the harness must refuse to start
an episode while that count is non-zero -- without ever turning an unanswerable
question into a missing row.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.eval import counting_proxy, run_suite
from scripts.eval.counting_proxy import Counters, Handler
from scripts.eval.run_suite import (
    IDLE_NOTE_S,
    IDLE_POLL_S,
    Runner,
    TaskSpec,
    wait_for_idle_upstream,
)


ANSWER = json.dumps(
    {
        "choices": [{"message": {"content": "done", "tool_calls": []}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }
).encode()


# --------------------------------------------------------------------------- #
# The counter itself
# --------------------------------------------------------------------------- #


def test_snapshot_reports_in_flight_beside_every_field_it_already_had() -> None:
    """The new field is additive: the harness reads the old ones from the same JSON."""
    snapshot = Counters().snapshot()

    assert snapshot["in_flight"] == 0
    assert set(snapshot) == {
        "requests",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "responses_without_usage",
        "upstream_errors",
        "late_responses",
        "in_flight",
    }


def test_an_answered_request_is_in_flight_only_while_the_model_is_working() -> None:
    counters = Counters()

    counters.start()
    assert counters.snapshot()["in_flight"] == 1

    counters.record(ANSWER, counters.epoch, b'{"messages":[]}')
    counters.finish()

    assert counters.snapshot()["in_flight"] == 0
    assert counters.snapshot()["requests"] == 1
    assert counters.snapshot()["total_tokens"] == 10


def test_a_failed_request_clears_the_slot_it_took() -> None:
    """An endpoint error ends the generation as surely as an answer does."""
    counters = Counters()

    counters.start()
    counters.error(counters.epoch)
    counters.finish()

    assert counters.snapshot()["in_flight"] == 0
    assert counters.snapshot()["upstream_errors"] == 1


def test_a_late_answer_clears_the_slot_although_its_task_is_gone() -> None:
    """The case this whole field exists for.

    The client is dead and the counters have been reset for the next task, so the reply
    is charged to nobody -- but the endpoint really is free again, and the next episode
    must be allowed to start.
    """
    counters = Counters()
    epoch = counters.epoch
    counters.start()

    counters.reset()  # the harness moves on to the next task
    assert counters.snapshot()["in_flight"] == 1, "the model is still generating"

    counters.record(ANSWER, epoch, b'{"messages":[]}')
    counters.finish()

    assert counters.snapshot()["in_flight"] == 0
    assert counters.snapshot()["late_responses"] == 1
    assert counters.snapshot()["requests"] == 0, "not charged to the new task"


def test_reset_between_tasks_does_not_erase_an_open_request() -> None:
    """Zeroing `in_flight` on reset would report a busy endpoint as idle."""
    counters = Counters()
    counters.start()
    counters.start()

    counters.reset()

    assert counters.snapshot()["in_flight"] == 2


def test_an_unpaired_finish_cannot_drive_the_count_below_zero() -> None:
    """A negative count would read as idle forever, which is the failure to avoid."""
    counters = Counters()

    counters.finish()
    counters.finish()

    assert counters.snapshot()["in_flight"] == 0

    counters.start()
    assert counters.snapshot()["in_flight"] == 1


def test_concurrent_requests_balance_out_exactly() -> None:
    """The proxy is threaded: `in_flight` is read and written from many threads."""
    counters = Counters()
    workers = 16
    per_worker = 200

    def churn() -> None:
        for _ in range(per_worker):
            counters.start()
            counters.finish()

    threads = [threading.Thread(target=churn) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert counters.snapshot()["in_flight"] == 0


# --------------------------------------------------------------------------- #
# The proxy end to end, with a client that walks away
# --------------------------------------------------------------------------- #


class SlowUpstream(BaseHTTPRequestHandler):
    """Stands in for the model: answers, but not immediately."""

    protocol_version = "HTTP/1.1"
    delay_s = 0.6

    def log_message(self, *_: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(self.delay_s)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(ANSWER)))
        self.end_headers()
        self.wfile.write(ANSWER)


class QuietServer(ThreadingHTTPServer):
    """Writing to a socket the client closed is the scenario, not a test failure."""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: object, client_address: object) -> None:
        pass


def serve(handler: type[BaseHTTPRequestHandler]) -> tuple[QuietServer, int]:
    server = QuietServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def until(predicate, deadline_s: float = 5.0) -> bool:
    limit = time.monotonic() + deadline_s
    while time.monotonic() < limit:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_a_client_that_disconnects_mid_answer_does_not_wedge_the_count(monkeypatch) -> None:
    """The real shape of a killed task, driven through the real handler.

    The agent's container is gone while the model is still generating. The proxy holds
    the upstream connection open on purpose -- that is how the late answer gets counted
    -- so the only thing that may not happen is `in_flight` staying stuck, which would
    make every later episode wait out the full idle bound for nothing.
    """
    counters = Counters()
    monkeypatch.setattr(counting_proxy, "COUNTERS", counters)

    upstream, upstream_port = serve(SlowUpstream)
    monkeypatch.setattr(Handler, "upstream", f"http://127.0.0.1:{upstream_port}")
    proxy, proxy_port = serve(Handler)
    try:
        body = b'{"messages":[{"role":"user","content":"hi"}]}'
        request = (
            "POST /v1/chat/completions HTTP/1.1\r\n"
            "Host: proxy\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode() + body
        client = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
        client.sendall(request)

        assert until(lambda: counters.snapshot()["in_flight"] == 1), "upstream never started"

        client.close()  # the task was killed; nobody is listening any more

        assert until(lambda: counters.snapshot()["in_flight"] == 0), "slot never released"
        assert counters.snapshot()["requests"] == 1, "the late answer is still counted"
        assert counters.snapshot()["total_tokens"] == 10
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_an_unreachable_upstream_releases_the_slot_too(monkeypatch) -> None:
    """The 502 path returns early; the count must still come back down."""
    counters = Counters()
    monkeypatch.setattr(counting_proxy, "COUNTERS", counters)
    # Port 1 on loopback refuses connections, so `urlopen` raises OSError at once.
    monkeypatch.setattr(Handler, "upstream", "http://127.0.0.1:1")
    proxy, proxy_port = serve(Handler)
    try:
        client = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
        client.sendall(b"GET /v1/models HTTP/1.1\r\nHost: proxy\r\n\r\n")
        assert until(lambda: counters.snapshot()["upstream_errors"] == 1)
        client.close()

        assert until(lambda: counters.snapshot()["in_flight"] == 0)
    finally:
        proxy.shutdown()
        proxy.server_close()


def test_reading_the_counters_is_not_itself_traffic(monkeypatch) -> None:
    """A poll must not register as in flight, or the wait would never end."""
    counters = Counters()
    monkeypatch.setattr(counting_proxy, "COUNTERS", counters)
    monkeypatch.setattr(Handler, "upstream", "http://127.0.0.1:1")
    proxy, proxy_port = serve(Handler)
    try:
        url = f"http://127.0.0.1:{proxy_port}/__counters"
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.loads(response.read())

        assert payload["in_flight"] == 0
        assert counters.snapshot()["in_flight"] == 0
        assert counters.snapshot()["upstream_errors"] == 0, "the poll never went upstream"
    finally:
        proxy.shutdown()
        proxy.server_close()


# --------------------------------------------------------------------------- #
# The harness side: waiting for the endpoint before an episode starts
# --------------------------------------------------------------------------- #


class FakeClock:
    """A clock that only moves when something sleeps, so the timing is exact."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def test_idle_wait_returns_at_once_when_the_endpoint_is_free() -> None:
    clock = FakeClock()

    waited, reason = wait_for_idle_upstream(
        lambda: {"in_flight": 0}, 240.0, now=clock.now, sleep=clock.sleep
    )

    assert (waited, reason) == (0.0, "idle")
    assert clock.slept == []


def test_idle_wait_polls_until_the_previous_episode_drains() -> None:
    clock = FakeClock()
    answers = [{"in_flight": 2}, {"in_flight": 1}, {"in_flight": 0}]

    waited, reason = wait_for_idle_upstream(
        lambda: answers.pop(0), 240.0, now=clock.now, sleep=clock.sleep
    )

    assert reason == "idle"
    assert waited == pytest.approx(2 * IDLE_POLL_S)
    assert clock.slept == [IDLE_POLL_S, IDLE_POLL_S]
    assert answers == []


def test_idle_wait_gives_up_at_the_maximum_and_lets_the_episode_run() -> None:
    """A wedged endpoint costs one episode's patience, not the row and not the night."""
    clock = FakeClock()

    waited, reason = wait_for_idle_upstream(
        lambda: {"in_flight": 1}, 5.0, now=clock.now, sleep=clock.sleep
    )

    assert (waited, reason) == (5.0, "timeout")
    assert clock.slept == [IDLE_POLL_S, IDLE_POLL_S, 1.0], "never sleeps past the bound"


def test_idle_wait_is_disabled_by_zero_without_asking_the_proxy() -> None:
    clock = FakeClock()
    asked = []

    waited, reason = wait_for_idle_upstream(
        lambda: asked.append(1) or {"in_flight": 9}, 0.0, now=clock.now, sleep=clock.sleep
    )

    assert (waited, reason) == (0.0, "disabled")
    assert asked == []
    assert clock.slept == []


@pytest.mark.parametrize(
    "answer",
    [None, {"requests": 3}, {"in_flight": "?"}, {"in_flight": None}, "not json"],
)
def test_idle_wait_proceeds_when_the_proxy_cannot_answer(answer) -> None:
    """No counter, or a proxy older than this field: unknown is not a reason to wait."""
    clock = FakeClock()

    waited, reason = wait_for_idle_upstream(
        lambda: answer, 240.0, now=clock.now, sleep=clock.sleep
    )

    assert (waited, reason) == (0.0, "unknown")
    assert clock.slept == []


def test_runner_skips_the_wait_when_no_counter_container_is_configured(tmp_path) -> None:
    runner = Runner(
        argparse.Namespace(out=str(tmp_path), counter_container="", idle_wait_max=240.0),
        tmp_path,
    )
    spec = TaskSpec("sample", tmp_path, "task", "/app", False, 10, 10, 10, 1, 512)

    assert runner.wait_for_idle(spec, 1) == (0.0, "disabled")


def test_runner_asks_the_proxy_for_in_flight_and_reports_the_wait(
    tmp_path, monkeypatch, capsys
) -> None:
    runner = Runner(
        argparse.Namespace(out=str(tmp_path), counter_container="proxy", idle_wait_max=90.0),
        tmp_path,
    )
    spec = TaskSpec("sample", tmp_path, "task", "/app", False, 10, 10, 10, 1, 512)
    asked: list[tuple[str, str]] = []
    runner.counter_call = lambda path, method: (  # type: ignore[assignment]
        asked.append((path, method)) or {"in_flight": 0}
    )
    seen: dict[str, object] = {}

    def fake_wait(counters, max_wait_s, **_):
        seen["max_wait_s"] = max_wait_s
        seen["snapshot"] = counters()
        return 12.5, "idle"

    monkeypatch.setattr(run_suite, "wait_for_idle_upstream", fake_wait)

    assert runner.wait_for_idle(spec, 2) == (12.5, "idle")
    assert seen == {"max_wait_s": 90.0, "snapshot": {"in_flight": 0}}
    assert asked == [("/__counters", "GET")]
    printed = capsys.readouterr().err
    assert "idle wait" in printed and "12.5" in printed and "sample r2" in printed


def test_a_probe_that_found_the_endpoint_free_is_not_reported_as_a_wait(
    tmp_path, monkeypatch, capsys
) -> None:
    """One `docker exec` costs a few hundred ms. That is the price of asking, and it
    must not appear on every row as though the episode had queued."""
    runner = Runner(
        argparse.Namespace(out=str(tmp_path), counter_container="proxy", idle_wait_max=240.0),
        tmp_path,
    )
    spec = TaskSpec("sample", tmp_path, "task", "/app", False, 10, 10, 10, 1, 512)
    monkeypatch.setattr(runner, "counter_call", lambda path, method: {"in_flight": 0})
    monkeypatch.setattr(
        run_suite, "wait_for_idle_upstream",
        lambda counters, max_wait_s, **_: (IDLE_NOTE_S / 2, "idle"),
    )

    runner.wait_for_idle(spec, 1)

    assert capsys.readouterr().err == ""


def test_a_proxy_that_cannot_answer_is_reported_even_without_a_wait(
    tmp_path, monkeypatch, capsys
) -> None:
    """A silent no-op guard would make an unprotected row look protected."""
    runner = Runner(
        argparse.Namespace(out=str(tmp_path), counter_container="proxy", idle_wait_max=240.0),
        tmp_path,
    )
    spec = TaskSpec("sample", tmp_path, "task", "/app", False, 10, 10, 10, 1, 512)
    monkeypatch.setattr(runner, "counter_call", lambda path, method: {"requests": 0})

    waited, reason = runner.wait_for_idle(spec, 1)

    assert (waited, reason) == (0.0, "unknown")
    assert "unknown" in capsys.readouterr().err


def test_a_hung_counter_exec_does_not_take_the_episode_down_with_it(
    tmp_path, monkeypatch
) -> None:
    """`counter_call` lets `docker exec` raise on timeout; the wait must absorb it."""
    runner = Runner(
        argparse.Namespace(out=str(tmp_path), counter_container="proxy", idle_wait_max=240.0),
        tmp_path,
    )
    spec = TaskSpec("sample", tmp_path, "task", "/app", False, 10, 10, 10, 1, 512)

    def hangs(path, method):
        raise subprocess.TimeoutExpired(["docker", "exec"], 60)

    monkeypatch.setattr(runner, "counter_call", hangs)

    assert runner.in_flight_snapshot() is None
    assert runner.wait_for_idle(spec, 1) == (0.0, "unknown")


def test_episode_waits_before_resetting_the_counters_and_records_the_wait(
    tmp_path, monkeypatch
) -> None:
    """Order matters: waiting after the reset would charge the tail to the new task."""
    args = argparse.Namespace(
        out=str(tmp_path), counter_container="proxy", idle_wait_max=240.0,
        timeout_scale=1, model="stub", base_url="local", api_key="", controller="",
        tell_agent_budget=False, network="none", reuse_images=True,
    )
    runner = Runner(args, tmp_path)
    spec = TaskSpec("sample", tmp_path, "task", "/app", False, 10, 10, 10, 1, 512)
    events: list[str] = []

    monkeypatch.setattr(runner, "build", lambda spec: None)
    monkeypatch.setattr(runner, "start_container", lambda spec: ("container", ""))
    monkeypatch.setattr(runner, "install", lambda container, spec: "")
    done = (
        "agent done: solved=False stop=steps stage=acted deliverable=none "
        "steps=30/30 recovery=0 tokens=15\n"
    )
    proc = subprocess.CompletedProcess([], 0, done, "")
    monkeypatch.setattr(runner, "exec_agent", lambda container, spec: (proc, 1.0, False))
    monkeypatch.setattr(runner, "verify", lambda container, spec: (None, "unreadable reward"))
    monkeypatch.setattr(
        runner, "counter_call", lambda path, method: events.append(path) or None
    )
    monkeypatch.setattr(
        run_suite, "wait_for_idle_upstream",
        lambda counters, max_wait_s, **_: (events.append("wait"), (7.5, "idle"))[1],
    )
    def fake_run(cmd, **kwargs):
        empty = "" if kwargs.get("text", True) else b""
        return subprocess.CompletedProcess(cmd, 0, empty, empty)

    monkeypatch.setattr(run_suite, "run", fake_run)

    row = runner.one(spec, 1)

    assert events[:2] == ["wait", "/__reset"]
    assert row.note == "idle_wait=7.5s unreadable reward", "both notes survive"


def test_an_expired_idle_wait_is_visible_in_the_row(tmp_path, monkeypatch) -> None:
    runner = Runner(
        argparse.Namespace(out=str(tmp_path), counter_container="proxy", idle_wait_max=240.0),
        tmp_path,
    )
    spec = TaskSpec("sample", tmp_path, "task", "/app", False, 10, 10, 10, 1, 512)
    monkeypatch.setattr(runner, "counter_call", lambda path, method: {"in_flight": 4})
    monkeypatch.setattr(
        run_suite, "wait_for_idle_upstream", lambda counters, max_wait_s, **_: (240.0, "timeout")
    )

    waited, reason = runner.wait_for_idle(spec, 1)

    assert (waited, reason) == (240.0, "timeout")
    note = f"idle_wait={waited:.1f}s" + ("" if reason == "idle" else f" ({reason})")
    assert note == "idle_wait=240.0s (timeout)"
    assert "\t" not in note and "\n" not in note, "a note shares a line with the row"


def test_results_table_keeps_its_column_set(tmp_path) -> None:
    """The idle wait rides in `note`; no reader's column indices may shift."""
    suite = run_suite.SuiteResult(
        label="x", model="m", base_url="u", timeout_scale=1.0, started="now",
        expected_tasks=["sample"], repeats=1,
    )
    suite.rows.append(
        run_suite.TaskResult(
            task="sample", repeat=1, reward=1, agent_exit=0, wall_s=1.0, tokens=10,
            steps=2, stop_reason="verified", solved_claim="True", workspace="/app",
            failure_mode="-", note="idle_wait=7.5s",
        )
    )

    run_suite.write_report(Path(tmp_path), suite)

    header, row = (tmp_path / "results.tsv").read_text().splitlines()[:2]
    assert header.split("\t") == [
        "task", "repeat", "reward", "tokens", "ep_tokens", "ep_calls", "ep_late",
        "wall_s", "steps", "stop", "claim", "wrote", "failure", "phases", "note",
        "agent_exit", "timed_out", "eligible_success",
    ]
    assert len(row.split("\t")) == len(header.split("\t"))
    assert "idle_wait=7.5s" in row


def test_help_mentions_the_idle_wait(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["run_suite.py", "--help"])

    with pytest.raises(SystemExit):
        run_suite.main()

    assert "idle" in capsys.readouterr().out
