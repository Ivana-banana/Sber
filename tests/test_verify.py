"""External verification: what decides `ok`, per `docs/research/verifier-contract.md`.

Every one of the six public verifiers reads the environment, never agent stdout, and
is binary on exit code / file state rather than on wording. These tests hold `verify()`
to the same rule: existence and content decide, a command's exit code decides, and an
empty verify command is a deliberate `unverified` pass, not a failure (M13: the real
verifier reads the environment regardless of what we check here).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.state import AgentState, Budget
from agent.verify import verify

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="target image is Linux; /bin/sh absent on Windows")


def make_state(tmp_path: Path, *, total_s: float = 600.0, secrets: tuple[str, ...] = ()) -> AgentState:
    state = AgentState(
        instruction="produce the deliverable",
        workspace=tmp_path,
        budget=Budget(total_s=total_s, clock=lambda: 0.0),
    )
    state.secrets = secrets
    return state


def test_a_workspace_relative_deliverable_resolves_against_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "deliverable.txt").write_text("OK", encoding="utf-8")
    state = make_state(tmp_path)

    result = verify(state, deliverable=Path("deliverable.txt"), command="")

    assert result.ok is True, "finish() gives a model-chosen path, which may be relative"
    assert result.stage == "unverified"


def test_a_deliverable_path_escaping_the_write_policy_says_so(tmp_path: Path) -> None:
    """Refused and absent are different facts, and the model acts on the difference.

    They used to share one message. `run_shell` is not confined to the workspace, so a
    run can genuinely create the artifact at the right absolute path -- and then be
    told the file it just wrote does not exist, which argues it into moving a correct
    answer somewhere wrong.
    """
    workspace = tmp_path / "app"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("OK", encoding="utf-8")
    state = make_state(workspace)

    result = verify(state, deliverable=Path("..") / "outside.txt", command="")

    assert result.ok is False
    assert result.stage == "refused"
    assert "not checked" in result.detail


def test_a_missing_deliverable_fails_at_existence(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    result = verify(state, deliverable=tmp_path / "report.json", command="")

    assert result.ok is False
    assert result.stage == "existence"
    assert "report.json" in result.detail


def test_a_directory_in_place_of_the_deliverable_fails_at_existence(tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    target.mkdir()
    state = make_state(tmp_path)

    result = verify(state, deliverable=target, command="")

    assert result.ok is False
    assert result.stage == "existence"


def test_an_empty_deliverable_fails_at_non_empty(tmp_path: Path) -> None:
    target = tmp_path / "deliverable.txt"
    target.write_text("", encoding="utf-8")
    state = make_state(tmp_path)

    result = verify(state, deliverable=target, command="")

    assert result.ok is False
    assert result.stage == "non-empty"


def test_a_present_non_empty_deliverable_with_no_command_is_unverified_but_ok(tmp_path: Path) -> None:
    target = tmp_path / "deliverable.txt"
    target.write_text("OK", encoding="utf-8")
    state = make_state(tmp_path)

    result = verify(state, deliverable=target, command="   ")

    assert result.ok is True, "the real verifier reads the environment, not us (M13)"
    assert result.stage == "unverified"


@POSIX_ONLY
def test_a_verify_command_that_exits_zero_passes(tmp_path: Path) -> None:
    target = tmp_path / "deliverable.txt"
    target.write_text("OK", encoding="utf-8")
    state = make_state(tmp_path)

    result = verify(state, deliverable=target, command="true")

    assert result.ok is True
    assert result.stage == "command"


@POSIX_ONLY
def test_a_verify_command_that_exits_non_zero_fails_even_if_it_prints_success(tmp_path: Path) -> None:
    target = tmp_path / "deliverable.txt"
    target.write_text("OK", encoding="utf-8")
    state = make_state(tmp_path)

    result = verify(state, deliverable=target, command="echo PASSED; exit 1")

    assert result.ok is False, "exit code decides, not the words the command printed"
    assert result.stage == "command"


@POSIX_ONLY
def test_a_deliverable_that_looks_right_but_fails_its_own_command_is_not_ok(tmp_path: Path) -> None:
    target = tmp_path / "deliverable.txt"
    target.write_text("OK\n", encoding="utf-8")
    state = make_state(tmp_path)

    result = verify(state, deliverable=target, command='[ "$(cat deliverable.txt)" = "OK" ]')

    assert result.ok is True

    target.write_text("OK extra\n", encoding="utf-8")
    result = verify(state, deliverable=target, command='[ "$(cat deliverable.txt)" = "OK" ]')

    assert result.ok is False
    assert result.stage == "command"


@POSIX_ONLY
def test_the_command_timeout_comes_from_the_budget_not_a_tool_default(tmp_path: Path) -> None:
    target = tmp_path / "deliverable.txt"
    target.write_text("OK", encoding="utf-8")
    # A near-exhausted budget leaves a sub-second tool timeout; the command sleeps
    # longer than that, so a passing test proves the budget's number was honoured.
    state = make_state(tmp_path, total_s=0.3)

    result = verify(state, deliverable=target, command="sleep 5")

    assert result.ok is False
    assert result.stage == "command"
    assert "timeout" in result.detail.lower()


@POSIX_ONLY
def test_secrets_configured_on_the_state_do_not_reach_the_detail(tmp_path: Path) -> None:
    target = tmp_path / "deliverable.txt"
    target.write_text("OK", encoding="utf-8")
    state = make_state(tmp_path, secrets=("sk-canary-0123456789",))

    result = verify(state, deliverable=target, command="echo sk-canary-0123456789; exit 1")

    assert "sk-canary-0123456789" not in result.detail


def test_a_deliverable_the_task_named_verifies_outside_the_workspace(tmp_path: Path) -> None:
    """Write policy and verification have to agree on where the answer may live.

    E011: the workspace was resolved to the agent's own directory, so a deliverable
    written where the task asked for it would have been refused here for existing in
    the right place. The two guards read the same policy now.
    """
    workspace = tmp_path / "opt" / "harbor" / "local-agent"
    workspace.mkdir(parents=True)
    named = tmp_path / "srv" / "bootstrap" / "farewell.marker"
    named.parent.mkdir(parents=True)
    named.write_bytes(b"Ciao")

    state = AgentState(
        instruction=f"Create {named} containing exactly Ciao.",
        workspace=workspace,
        budget=Budget(total_s=600.0, clock=lambda: 0.0),
        named_paths=(named,),
    )

    result = verify(state, deliverable=named, command="")

    assert result.ok is True
    assert result.stage == "unverified"


def test_a_deliverable_nobody_named_is_still_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere" / "loot.txt"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("x", encoding="utf-8")

    state = AgentState(
        instruction="Write /app/report.json with the findings.",
        workspace=workspace,
        budget=Budget(total_s=600.0, clock=lambda: 0.0),
        named_paths=(Path("/app/report.json"),),
    )

    result = verify(state, deliverable=elsewhere, command="")

    assert result.ok is False
    assert result.stage == "refused"


def test_an_untouched_input_file_is_not_a_deliverable(tmp_path: Path) -> None:
    """The cheapest way to fake success is to point `finish` at the task's own input.

    Every input file exists and is non-empty, which is all the earlier stages ask for.
    Widening the write policy to paths the task names made it cheaper still: any
    existing file the text mentions would have passed.
    """
    from agent.paths import snapshot

    stale = tmp_path / "input.py"
    stale.write_text("def add(a, b): return a - b\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.baseline = snapshot((stale,))

    result = verify(state, deliverable=stale, command="")

    assert result.ok is False
    assert result.stage == "stale"


def test_touching_an_input_file_does_not_make_it_a_deliverable(tmp_path: Path) -> None:
    """Provenance is decided by content, not by timestamps.

    The first version of this rule compared mtime against the run's start, which both
    a `touch` on an input and a `tar -xf` of real evidence could defeat -- in opposite
    directions.
    """
    import os
    import time

    from agent.paths import snapshot

    stale = tmp_path / "input.py"
    stale.write_text("unchanged\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.baseline = snapshot((stale,))
    later = time.time() + 60
    os.utime(stale, (later, later))

    result = verify(state, deliverable=stale, command="")

    assert result.stage == "stale"


def test_an_artifact_restored_with_its_old_timestamp_still_counts(tmp_path: Path) -> None:
    """`tar -xf`, `cp -p` and `unzip` restore mtimes, and forensic tasks unpack evidence.

    A deliverable that looks older than the run is not thereby someone else's work.
    """
    import os
    import time

    from agent.paths import snapshot

    target = tmp_path / "incident_report.txt"
    state = make_state(tmp_path)
    state.baseline = snapshot((target,))  # absent at start
    target.write_text("findings\n", encoding="utf-8")
    long_ago = time.time() - 86400
    os.utime(target, (long_ago, long_ago))

    result = verify(state, deliverable=target, command="")

    assert result.ok is True
    assert result.stage == "unverified"


def test_a_file_this_run_wrote_is_accepted(tmp_path: Path) -> None:
    from agent.paths import snapshot

    fresh = tmp_path / "report.json"
    state = make_state(tmp_path)
    state.baseline = snapshot((fresh,))
    fresh.write_text('{"findings": []}', encoding="utf-8")

    result = verify(state, deliverable=fresh, command="")

    assert result.ok is True
    assert result.stage == "unverified"


def test_an_empty_stale_file_is_diagnosed_as_empty(tmp_path: Path) -> None:
    # Ordering: being told a file is empty is more actionable than being told it is
    # unchanged, and an empty file is unchanged in every run that did not write it.
    from agent.paths import snapshot

    empty = tmp_path / "report.json"
    empty.touch()
    state = make_state(tmp_path)
    state.baseline = snapshot((empty,))

    result = verify(state, deliverable=empty, command="")

    assert result.stage == "non-empty"


@POSIX_ONLY
def test_a_failed_check_reports_why_it_failed(tmp_path: Path) -> None:
    """A missing verifier binary must reach recovery as a fact, not bare `failed`."""
    state = make_state(tmp_path)
    target = tmp_path / "answer.txt"
    target.write_text("candidate\n")
    state.artifacts.append(target)

    verdict = verify(state, deliverable=target, command="definitely-not-a-real-binary --check")

    assert not verdict.ok
    assert verdict.stage == "command"
    assert "definitely-not-a-real-binary" in verdict.detail
    assert "not found" in verdict.detail.lower()


@POSIX_ONLY
def test_a_failed_check_output_is_bounded_and_redacted(tmp_path: Path) -> None:
    state = make_state(tmp_path, secrets=("sk-canary-value",))
    target = tmp_path / "answer.txt"
    target.write_text("candidate\n")
    state.artifacts.append(target)

    verdict = verify(
        state,
        deliverable=target,
        command=(
            "printf '\\141\\163\\163\\145\\162\\164\\151\\157\\156 "
            "\\144\\145\\164\\141\\151\\154'; "
            "printf 'sk-canary-%s' value >&2; yes Z | head -c 5000; exit 1"
        ),
    )

    assert not verdict.ok
    assert "assertion detail" in verdict.detail
    assert "sk-canary-value" not in verdict.detail
    assert len(verdict.detail) < 1800


@POSIX_ONLY
def test_failed_check_detail_is_one_line_for_telemetry(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    target = tmp_path / "answer.txt"
    target.write_text("candidate\n")
    state.artifacts.append(target)

    verdict = verify(
        state,
        deliverable=target,
        command="printf 'first\\nsecond\\n'; exit 1",
    )

    assert not verdict.ok
    assert "first\\nsecond" in verdict.detail
    assert "\n" not in verdict.detail
    assert "\r" not in verdict.detail


def test_an_exact_file_requirement_rejects_wrong_bytes_without_a_model_check(
    tmp_path: Path,
) -> None:
    target = tmp_path / "answer.txt"
    state = AgentState(
        instruction=(
            f"Create a file at `{target}` whose entire content is exactly the single "
            "word `Expected` (the file must contain only those eight characters)."
        ),
        workspace=tmp_path,
        budget=Budget(total_s=600.0, clock=lambda: 0.0),
        named_paths=(target,),
    )
    from agent.paths import snapshot

    state.baseline = snapshot((target,))
    target.write_bytes(b"Wrong")

    result = verify(state, deliverable=target, command="")

    assert result.ok is False
    assert result.stage == "exact-file"


def test_an_exact_file_requirement_accepts_exact_bytes_without_a_model_check(
    tmp_path: Path,
) -> None:
    target = tmp_path / "answer.txt"
    state = AgentState(
        instruction=(
            f"Create a file at `{target}` whose entire content is exactly the single "
            "word `Expected` (the file must contain only those eight characters)."
        ),
        workspace=tmp_path,
        budget=Budget(total_s=600.0, clock=lambda: 0.0),
        named_paths=(target,),
    )
    from agent.paths import snapshot

    state.baseline = snapshot((target,))
    target.write_bytes(b"Expected")

    result = verify(state, deliverable=target, command="")

    assert result.ok is True
    assert result.stage == "exact-file"


@POSIX_ONLY
def test_an_exact_file_requirement_does_not_run_the_model_verify_command(
    tmp_path: Path,
) -> None:
    target = tmp_path / "answer.txt"
    marker = tmp_path / "side-effect.txt"
    state = AgentState(
        instruction=(
            f"Create a file at `{target}` whose entire content is exactly the single "
            "word `Expected` (the file must contain only those eight characters)."
        ),
        workspace=tmp_path,
        budget=Budget(total_s=600.0, clock=lambda: 0.0),
        named_paths=(target,),
    )
    target.write_bytes(b"Expected")

    result = verify(state, deliverable=target, command=f"touch {marker}")

    assert result.ok is True
    assert result.stage == "exact-file"
    assert not marker.exists()


# --- the environment the check runs in --------------------------------------
#
# Measured, twelve episodes of two public code-fix tasks (JOURNAL, 16.09.2026): the
# model's verify command was the project's own HTTP test suite every time, and the
# container had started the service once at entry without reload -- so the suite
# measured the file as it was before the run edited it. Four episodes finished
# `solved=True` on code whose endpoint answered 500, and the one episode that had
# really repaired the file was recorded as failed.
#
# These tests use a real service in a real subprocess for that reason: a stub cannot
# fail the way this failed. The service reads its behaviour from a module it imports at
# startup, which is exactly how a process keeps serving pre-change code.

import contextlib
import http.client
import signal
import socket
import subprocess
import sys
import time

from agent import result_progress, tools
from agent import verify as verify_module

LINUX_ONLY = pytest.mark.skipif(
    not Path("/proc/self/stat").exists(), reason="procfs is a Linux interface"
)

SERVICE_SOURCE = """\
import http.server
import os
import sys

import setting

PORT = int(sys.argv[1])


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"{setting.VERSION}|{os.environ.get('SERVICE_MARK', '')}".encode()
        self.send_response(setting.STATUS)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


with open("started.txt", "a", encoding="utf-8") as handle:
    handle.write(f"{os.getpid()}\\n")
http.server.HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
"""

# Starts the service and exits at once, so the service is reparented to init and is not
# a descendant of the test runner -- the shape the container's entrypoint produces, and
# the only shape the reload is allowed to touch.
LAUNCHER_SOURCE = """\
import subprocess
import sys

child = subprocess.Popen(
    [sys.executable, "service.py", sys.argv[1]],
    cwd=sys.argv[2],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
sys.stdout.write(str(child.pid))
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_until(predicate, *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class Service:
    """A real HTTP service in its own process, written into a real directory."""

    def __init__(self, directory: Path, *, status: int = 200, version: str = "old") -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "service.py").write_text(SERVICE_SOURCE, encoding="utf-8")
        self.setting = directory / "setting.py"
        self.write_setting(status=status, version=version)
        self.port = free_port()
        self.started: list[int] = []
        self.children: list[subprocess.Popen] = []

    def write_setting(self, *, status: int = 200, version: str = "old") -> None:
        self.setting.write_text(f"STATUS = {status}\nVERSION = {version!r}\n", encoding="utf-8")

    def start(self, *, own_child: bool = False) -> int:
        """Start the service. `own_child` keeps it inside the test's process tree."""
        argv = [sys.executable, "service.py", str(self.port)]
        environment = {
            **os.environ,
            "SERVICE_MARK": "kept-through-restart",
            # Without this the interpreter reuses `__pycache__/setting.pyc`: its
            # validity check is (mtime seconds, size), and an edit of the same length
            # inside the same second passes it. Real repairs change the length; this
            # keeps the test measuring the reload rather than the bytecode cache.
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if own_child:
            child = subprocess.Popen(
                argv,
                cwd=self.directory,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.children.append(child)
            pid = child.pid
        else:
            launched = subprocess.run(
                [sys.executable, "-c", LAUNCHER_SOURCE, str(self.port), str(self.directory)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15.0,
            )
            pid = int(launched.stdout.strip())
        self.started.append(pid)
        assert wait_until(lambda: tools.port_open("127.0.0.1", self.port, timeout_s=0.2)), (
            "the service never came up"
        )
        return pid

    def pids(self) -> list[int]:
        """Every pid that has served on this port, in order, from the service's own log."""
        try:
            lines = (self.directory / "started.txt").read_text(encoding="utf-8").split()
        except OSError:
            return []
        return [int(line) for line in lines]

    def body(self) -> str:
        with contextlib.closing(http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)) as link:
            link.request("GET", "/")
            return link.getresponse().read().decode("utf-8")

    def stop(self) -> None:
        """Leave nothing behind: the reload starts processes this object never spawned.

        Repeated until the port is free, because `started.txt` is what names them and a
        restart can append to it while the first pass is still killing.
        """
        for _ in range(4):
            for pid in {*self.started, *self.pids()}:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
            if not tools.port_open("127.0.0.1", self.port, timeout_s=0.1):
                break
            time.sleep(0.05)
        for child in self.children:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                child.wait(timeout=2.0)


@pytest.fixture
def service(tmp_path: Path):
    running = Service(tmp_path / "app")
    try:
        yield running
    finally:
        running.stop()


def code_fix_state(workspace: Path, *, budget_s: float = 600.0) -> AgentState:
    """A run that has already edited production source, snapshot taken before the edit."""
    state = AgentState(
        instruction="repair the service",
        workspace=workspace,
        budget=Budget(total_s=budget_s, clock=lambda: 0.0),
        source_snapshot=result_progress.snapshot_sources(workspace),
    )
    return state


@LINUX_ONLY
def test_a_service_running_pre_change_code_is_reloaded_before_the_command_runs(service) -> None:
    """The measured defect, end to end: the command only sees the fix after a reload."""
    state = code_fix_state(service.directory)
    first = service.start()
    service.write_setting(version="new")
    state.artifacts.append(service.setting)
    check = (
        f"{sys.executable} -c \"import urllib.request; "
        f"assert b'new' in urllib.request.urlopen('http://127.0.0.1:{service.port}/').read()\""
    )

    result = verify(state, deliverable=service.setting, command=check)

    assert result.ok is True, result.detail
    assert result.stage == "command"
    assert "reloaded" in result.detail
    assert service.pids()[-1] != first, "the old process was still serving the old module"
    assert not tools.process_alive(first)
    assert "kept-through-restart" in service.body(), "argv, cwd and environment are re-used"


@LINUX_ONLY
def test_a_service_that_answers_5xx_after_the_change_fails_whatever_the_command_says(service) -> None:
    state = code_fix_state(service.directory)
    service.start()
    service.write_setting(status=500, version="new")
    state.artifacts.append(service.setting)

    result = verify(state, deliverable=service.setting, command="true")

    assert result.ok is False, "`true` exits 0; the port is what decides"
    assert result.stage == "liveness"
    assert str(service.port) in result.detail
    assert "500" in result.detail


@LINUX_ONLY
def test_a_service_already_broken_before_the_change_is_not_called_a_regression(service) -> None:
    """A false red costs a solved task, so only a status the change introduced is red."""
    service.write_setting(status=500, version="old")  # broken before the run touched it
    state = code_fix_state(service.directory)
    service.start()
    service.write_setting(status=500, version="new")
    state.artifacts.append(service.setting)

    result = verify(state, deliverable=service.setting, command="true")

    assert result.ok is True, result.detail
    assert "as it did before the reload" in result.detail


@LINUX_ONLY
def test_a_service_that_does_not_come_back_is_a_failure_the_loop_can_recover_from(
    service, monkeypatch
) -> None:
    monkeypatch.setattr(verify_module, "RELOAD_PORT_WAIT_S", 0.5)
    state = code_fix_state(service.directory)
    service.start()
    service.setting.write_text("raise RuntimeError('broken import')\n", encoding="utf-8")
    state.artifacts.append(service.setting)

    result = verify(state, deliverable=service.setting, command="true")

    assert result.ok is False
    assert result.stage == "liveness"
    assert "did not listen again" in result.detail
    assert str(service.port) in result.detail


@LINUX_ONLY
def test_a_service_that_does_not_reference_the_change_is_left_running(tmp_path: Path) -> None:
    elsewhere = Service(tmp_path / "unrelated")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    edited = workspace / "module.py"
    edited.write_text("VALUE = 1\n", encoding="utf-8")
    state = code_fix_state(workspace)
    try:
        first = elsewhere.start()
        edited.write_text("VALUE = 2\n", encoding="utf-8")
        state.artifacts.append(edited)

        result = verify(state, deliverable=edited, command="true")

        assert result.ok is True
        assert "reloaded" not in result.detail
        assert elsewhere.pids() == [first], "an unrelated service must not be restarted"
        assert tools.process_alive(first)
    finally:
        elsewhere.stop()


@LINUX_ONLY
def test_the_agents_own_process_tree_is_never_reloaded(service) -> None:
    """Our own children are ours: restarting one would kill the run's own tooling."""
    state = code_fix_state(service.directory)
    first = service.start(own_child=True)
    service.write_setting(version="new")
    state.artifacts.append(service.setting)

    result = verify(state, deliverable=service.setting, command="true")

    assert result.ok is True
    assert "reloaded" not in result.detail
    assert service.pids() == [first]
    assert tools.process_alive(first)


def test_pid_one_and_this_process_are_protected_whatever_the_table_says() -> None:
    protected = verify_module._protected_pids(tools.process_table())

    assert 1 in protected, "PID 1 holds the container open; killing it ends the task"
    assert os.getpid() in protected


@LINUX_ONLY
def test_an_unreadable_environment_leaves_the_service_alone_and_says_so(service, monkeypatch) -> None:
    """A permission refusal degrades to a note. Restarting into a stripped environment
    would break a service for a reason the run did not cause."""
    monkeypatch.setattr(tools, "process_environ", lambda pid, **kwargs: None)
    state = code_fix_state(service.directory)
    first = service.start()
    service.write_setting(version="new")
    state.artifacts.append(service.setting)

    result = verify(state, deliverable=service.setting, command="true")

    assert result.ok is True
    assert "environment is unreadable" in result.detail
    assert service.pids() == [first]
    assert tools.process_alive(first)


def test_a_missing_process_table_degrades_to_a_note_not_a_crash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tools, "PROC_ROOT", tmp_path / "no-procfs")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    edited = workspace / "module.py"
    edited.write_text("VALUE = 1\n", encoding="utf-8")
    state = code_fix_state(workspace)
    edited.write_text("VALUE = 2\n", encoding="utf-8")
    state.artifacts.append(edited)

    result = verify(state, deliverable=edited, command="true")

    assert result.ok is True
    assert "process table is unreadable" in result.detail


def test_a_run_that_changed_nothing_running_reloads_nothing(tmp_path: Path) -> None:
    """A report is not a repair: a file this run created cannot be loaded by a process
    that started before it, so nothing is restarted for it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = code_fix_state(workspace)
    report = workspace / "report.json"
    report.write_text('{"findings": []}', encoding="utf-8")
    state.artifacts.append(report)

    assert verify_module._modified_sources(state) == ()


def test_only_files_that_existed_before_the_run_count_as_changed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    edited = workspace / "module.py"
    edited.write_text("VALUE = 1\n", encoding="utf-8")
    state = code_fix_state(workspace)
    edited.write_text("VALUE = 2\n", encoding="utf-8")
    (workspace / "created.py").write_text("NEW = True\n", encoding="utf-8")

    assert verify_module._modified_sources(state) == (edited.resolve(),)


def test_the_caller_may_name_the_changed_files_itself(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    named = tmp_path / "given.py"
    named.write_text("VALUE = 1\n", encoding="utf-8")

    assert verify_module._modified_sources(state, [named]) == (named.resolve(),)


@POSIX_ONLY
def test_a_repeat_of_a_command_that_already_passed_says_the_suite_may_not_be_idempotent(
    tmp_path: Path,
) -> None:
    """Measured: one project's suite inserted a fixed row, so its second identical run
    always failed -- and that burned the single recovery pass on a correct file."""
    target = tmp_path / "answer.txt"
    target.write_text("candidate\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.artifacts.append(target)
    command = "false"
    state.record("verify", "command", verify_module._success_detail(command))

    result = verify(state, deliverable=target, command=command)

    assert result.ok is False, "a repeated suite is never silently treated as success"
    assert "idempotent" in result.detail


@POSIX_ONLY
def test_a_different_command_is_not_called_non_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "answer.txt"
    target.write_text("candidate\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.record("verify", "command", verify_module._success_detail("false --quiet"))

    result = verify(state, deliverable=target, command="false")

    assert result.ok is False
    assert "idempotent" not in result.detail, "a prefix of another command is another command"


@POSIX_ONLY
def test_a_caller_with_its_own_history_may_state_the_repeat_itself(tmp_path: Path) -> None:
    target = tmp_path / "answer.txt"
    target.write_text("candidate\n", encoding="utf-8")
    state = make_state(tmp_path)

    result = verify(state, deliverable=target, command="false", already_passed=True)

    assert result.ok is False
    assert "idempotent" in result.detail


def test_a_passing_detail_stays_recognisable_when_a_reload_note_is_appended() -> None:
    """The trace is where the repeat is read from, so the two parts must stay separable."""
    detail = verify_module._with_note(verify_module._success_detail("pytest tests/"), "reloaded x")

    assert detail.startswith(verify_module._success_detail("pytest tests/"))
    assert "reloaded x" in detail


@POSIX_ONLY
def test_a_suite_the_model_itself_ran_green_is_the_repeat_that_was_measured(tmp_path: Path) -> None:
    """The measured non-idempotent run was the model's own `run_shell`, then `finish`.

    So the action history is where the first green run lives -- it never reached the
    verify trace, because a passing verification ends the run.
    """
    target = tmp_path / "answer.txt"
    target.write_text("candidate\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.observe("run_shell", {"command": "false"}, "1 passed", ok=True)

    result = verify(state, deliverable=target, command="false")

    assert result.ok is False
    assert "idempotent" in result.detail


@POSIX_ONLY
def test_a_command_the_model_ran_and_failed_is_not_called_non_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "answer.txt"
    target.write_text("candidate\n", encoding="utf-8")
    state = make_state(tmp_path)
    state.observe("run_shell", {"command": "false"}, "1 failed", ok=False)

    result = verify(state, deliverable=target, command="false")

    assert result.ok is False
    assert "idempotent" not in result.detail


@POSIX_ONLY
def test_a_broken_process_table_reader_cannot_crash_the_verification(
    tmp_path: Path, monkeypatch
) -> None:
    """A crash here is a task scored zero; a note is a task that can still finish."""

    def explode(**kwargs: object) -> None:
        raise RuntimeError("procfs went away mid-scan")

    monkeypatch.setattr(tools, "process_table", explode)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    edited = workspace / "module.py"
    edited.write_text("VALUE = 1\n", encoding="utf-8")
    state = code_fix_state(workspace)
    edited.write_text("VALUE = 2\n", encoding="utf-8")
    state.artifacts.append(edited)

    result = verify(state, deliverable=edited, command="true")

    assert result.ok is True
    assert "RuntimeError" in result.detail


def application_tree(root: Path) -> tuple[Path, Path]:
    """The shape the public code-fix tasks run: an entry module beside a package."""
    (root / "routers").mkdir(parents=True)
    entry = root / "main.py"
    entry.write_text("from routers import auth\n", encoding="utf-8")
    changed = root / "routers" / "auth.py"
    changed.write_text("QUERY = 'select 1'\n", encoding="utf-8")
    return entry, changed


def process_at(root: Path, *argv: str, pid: int = 4242) -> tools.ProcessInfo:
    return tools.ProcessInfo(pid=pid, ppid=1, argv=argv, cwd=root, started_at=1.0)


def test_a_server_started_as_a_module_from_the_application_root_is_matched(tmp_path: Path) -> None:
    """`uvicorn main:app` names no router, but it imports every file beside it."""
    _, changed = application_tree(tmp_path)

    reason = verify_module._references(
        process_at(tmp_path, "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"),
        (changed,),
    )

    assert "auth.py" in reason


def test_a_dotted_package_entry_point_is_matched(tmp_path: Path) -> None:
    root = tmp_path / "srv"
    _, changed = application_tree(root / "api")

    reason = verify_module._references(
        process_at(root, "gunicorn", "api.main:app", "--workers", "2"), (changed,)
    )

    assert "auth.py" in reason


def test_a_script_named_by_absolute_path_is_matched_without_a_useful_cwd(tmp_path: Path) -> None:
    entry, changed = application_tree(tmp_path)
    process = tools.ProcessInfo(pid=77, ppid=1, argv=("node", str(entry)), cwd=Path("/"), started_at=1.0)

    reason = verify_module._references(process, (changed,))

    assert "auth.py" in reason


def test_a_shell_sitting_in_the_application_root_is_not_a_service(tmp_path: Path) -> None:
    _, changed = application_tree(tmp_path)

    assert verify_module._references(process_at(tmp_path, "-bash"), (changed,)) == ""
    assert verify_module._references(process_at(tmp_path, "sleep", "300"), (changed,)) == ""


def test_a_tail_following_a_log_in_the_application_root_is_not_a_service(tmp_path: Path) -> None:
    _, changed = application_tree(tmp_path)
    log = tmp_path / "access.log"
    log.write_text("started\n", encoding="utf-8")

    assert verify_module._references(process_at(tmp_path, "tail", "-f", str(log)), (changed,)) == ""


def test_a_process_in_another_tree_is_not_matched(tmp_path: Path) -> None:
    _, changed = application_tree(tmp_path / "app")
    other = tmp_path / "other"
    (other / "routers").mkdir(parents=True)
    (other / "main.py").write_text("x = 1\n", encoding="utf-8")

    assert verify_module._references(process_at(other, "uvicorn", "main:app"), (changed,)) == ""
