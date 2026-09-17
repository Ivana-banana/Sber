"""Проверка, которая не могла увидеть правку, не считается проверкой.

Измерено на стенде qwen3:30b-a3b (три эпизода code-fix, `build/afk-30b-treatment4`):
модель правила обработчик, запускала тесты проекта, тесты проходили — и проходили они
против сервиса, поднятого при старте контейнера без reload, то есть против кода *до*
правки. Две правки из трёх были при этом неверны. Агент сообщал solved=True, а грейдер,
который перезапускает сервис сам, ставил ноль.

Здесь это воспроизводится настоящими процессами и настоящим procfs: сервис стартует
раньше правки и остаётся жив, петля доходит до `finish`, и первый `finish` должен быть
отклонён с наблюдением, называющим pid, порт и время. Второй `finish` проходит — решение
за моделью, а в аудите остаётся запись.

Ничего не перезапускается и не убивается: этот шлюз только смотрит и говорит.
Разрушительная половина (авто-перезапуск сервиса) была отклонена ревью и не вернулась.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent import limits, loop, playbooks, prompt, result_progress, tools
from agent.state import AgentState
from tests.stub_llm import StubLlm
from tests.test_loop import FakeVerify, action, make_state, run
from tests.test_tools import BOOT, FakeProcess, write_proc

LINUX_ONLY = pytest.mark.skipif(
    not Path("/proc/self/stat").exists(),
    reason="procfs is a Linux interface; the target image is Linux",
)

SERVICE_SOURCE = """\
import http.server
import sys

import routers.auth


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = str(routers.auth.login()).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""

LAUNCHER_SOURCE = """\
import subprocess
import sys

child = subprocess.Popen(
    [sys.executable, sys.argv[1], sys.argv[2]],
    cwd=sys.argv[3],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
sys.stdout.write(str(child.pid))
"""
"""Starts the service and exits at once, so the service is reparented to init.

That is the shape the container's entrypoint produces, and the shape that matters: a
process inside the agent's own tree is deliberately invisible to the scan, so a test
that kept the service as its own child would prove nothing about the real case.
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def write_app(root: Path) -> Path:
    """The shape of the public applications: an entry module beside a router package."""
    (root / "routers").mkdir(parents=True, exist_ok=True)
    changed = root / "routers" / "auth.py"
    changed.write_text("def login():\n    return 'old'\n", encoding="utf-8")
    (root / "main.py").write_text(SERVICE_SOURCE, encoding="utf-8")
    return changed


class Machine:
    """Every process a test started, so the teardown can leave nothing behind."""

    def __init__(self) -> None:
        self.pids: list[int] = []
        self.children: list[subprocess.Popen[bytes]] = []

    def detached(self, directory: Path) -> tuple[int, int]:
        """A service outside the agent's process tree, as the entrypoint would start it."""
        port = free_port()
        launched = subprocess.run(
            [sys.executable, "-c", LAUNCHER_SOURCE, "main.py", str(port), str(directory)],
            capture_output=True,
            text=True,
            timeout=30.0,
        )
        assert launched.returncode == 0, launched.stderr
        pid = int(launched.stdout.strip())
        self.pids.append(pid)
        assert wait_for_port(port), "the service never came up"
        return pid, port

    def own_child(self, directory: Path) -> int:
        """A service the agent started itself. It already has the change, by definition."""
        port = free_port()
        child = subprocess.Popen(
            [sys.executable, "main.py", str(port)],
            cwd=str(directory),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.children.append(child)
        self.pids.append(child.pid)
        assert wait_for_port(port), "the agent's own service never came up"
        return child.pid

    def stop(self) -> None:
        for pid in self.pids:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        for child in self.children:
            try:
                child.wait(timeout=5.0)
            except (OSError, subprocess.TimeoutExpired):
                pass


@pytest.fixture
def machine():
    running = Machine()
    try:
        yield running
    finally:
        running.stop()


def wait_for_port(port: int, *, timeout_s: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                return True
        except OSError:
            time.sleep(0.02)
    return False


def started_at(pid: int) -> float:
    """When procfs says this process started. Read, not assumed: `/proc/stat`'s boot
    time is a whole second, so a start reconstructed from it carries up to a second of
    error, and a test that guessed instead would be flaky exactly at the margin."""
    for info in tools.process_table(max_processes=1_000_000).processes:
        if info.pid == pid:
            assert info.started_at > 0.0, "procfs did not report a start time"
            return info.started_at
    raise AssertionError(f"pid {pid} is not in the process table")


def wait_until_an_edit_is_newer(pid: int) -> None:
    """Block until a file written now counts as changed after `pid` started."""
    target = started_at(pid) + limits.SERVICE_SCAN_START_MARGIN_S + 0.25
    while time.time() <= target:
        time.sleep(0.02)


def codefix_state(root: Path) -> AgentState:
    state = make_state(root)
    state.source_snapshot = result_progress.snapshot_sources(root)
    return state


def fix(changed: Path) -> object:
    return action("replace_text", path=str(changed), old="return 'old'", new="return 'new'")


def finish(changed: Path) -> object:
    return action("finish", path=str(changed), verify="pytest -q", summary="исправлено")


def hints(outcome) -> list[str]:
    return [text for text in outcome.observations if "[verify] сервис" in text]


def notes(state: AgentState, name: str) -> list[str]:
    return [event.detail for event in state.audit if event.kind == "verify" and event.name == name]


def run_fix(state: AgentState, changed: Path, *, finishes: int = 2, verdicts: int = 1):
    llm = StubLlm(
        replies=[fix(changed), *[finish(changed) for _ in range(finishes)]]  # type: ignore[list-item]
    )
    verify = FakeVerify(*[(True, "command", "") for _ in range(verdicts)])
    return run(state, llm, verify, playbook=playbooks.CODE_FIX), verify


# --- what the gate must catch -------------------------------------------------


@LINUX_ONLY
def test_finish_is_refused_once_while_a_service_older_than_the_edit_runs(
    tmp_path: Path, machine: Machine
) -> None:
    changed = write_app(tmp_path)
    service_pid, port = machine.detached(tmp_path)
    own_pid = machine.own_child(tmp_path)
    wait_until_an_edit_is_newer(service_pid)
    state = codefix_state(tmp_path)

    outcome, verify = run_fix(state, changed)

    said = hints(outcome)
    assert len(said) == 1, "refused once and only once; the second finish is the model's call"
    assert f"pid {service_pid}" in said[0], said[0]
    assert f":{port}" in said[0], "the port is what has to answer again after a restart"
    assert "routers/auth.py" in said[0], "name the edit the check could not have seen"
    assert str(own_pid) not in said[0], "a service the agent started itself already has the change"
    assert "Ещё" not in said[0], "only the pre-change service is stale"
    assert "entrypoint" in said[0], "say what to do, not only that something is wrong"

    assert outcome.solved is True, "the second finish goes through -- the model decided"
    assert len(verify.calls) == 1, "the refused finish never reached the verifier"
    assert len(notes(state, "stale-service")) == 2, "recorded when refused and when let through"


@LINUX_ONLY
def test_the_refusal_costs_one_step_and_never_touches_the_service(
    tmp_path: Path, machine: Machine
) -> None:
    # The line the reverted attempt crossed. This gate observes; the service it names is
    # still running, unchanged, when the run ends.
    changed = write_app(tmp_path)
    service_pid, port = machine.detached(tmp_path)
    wait_until_an_edit_is_newer(service_pid)
    state = codefix_state(tmp_path)

    outcome, _ = run_fix(state, changed)

    assert outcome.solved is True
    assert state.budget.steps_used == 3, "edit, refused finish, accepted finish"
    assert tools.process_table(max_processes=1_000_000).processes, "procfs is still readable"
    assert any(
        info.pid == service_pid for info in tools.process_table(max_processes=1_000_000).processes
    ), "the service was never signalled"
    assert wait_for_port(port, timeout_s=2.0), "and it is still serving"


# --- and what it must never catch ---------------------------------------------


@LINUX_ONLY
def test_a_service_started_after_the_edit_does_not_refuse_finish(
    tmp_path: Path, machine: Machine
) -> None:
    # A service that restarted itself after the change already has the change. Refusing
    # here would cost a correct run a step and hand the model a false observation.
    changed = write_app(tmp_path)
    state = codefix_state(tmp_path)
    changed.write_text("def login():\n    return 'new'\n", encoding="utf-8")
    state.artifacts.append(changed)
    # Started only now, so it imported the repaired module -- the shape of a service
    # that restarts itself, and of one the model restarted when told to.
    machine.detached(tmp_path)

    llm = StubLlm(replies=[finish(changed)])  # type: ignore[list-item]
    outcome = run(state, llm, FakeVerify((True, "command", "")), playbook=playbooks.CODE_FIX)

    assert hints(outcome) == []
    assert notes(state, "stale-service") == []
    assert outcome.solved is True


@LINUX_ONLY
def test_a_process_that_only_names_the_workspace_directory_does_not_refuse_finish(
    tmp_path: Path, machine: Machine
) -> None:
    # `mysqld --datadir=/app` is the measured shape of this false positive: a process
    # pointed at the application directory that has never imported a line of it.
    changed = write_app(tmp_path)
    outside = tmp_path.parent / "sidecar"
    outside.mkdir(exist_ok=True)
    (outside / "store.py").write_text(
        "import sys, time\nwhile True:\n    time.sleep(0.2)\n", encoding="utf-8"
    )
    child = subprocess.Popen(
        [sys.executable, str(outside / "store.py"), f"--datadir={tmp_path}"],
        cwd=str(outside),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    machine.children.append(child)
    machine.pids.append(child.pid)
    wait_until_an_edit_is_newer(child.pid)
    state = codefix_state(tmp_path)

    outcome, _ = run_fix(state, changed, finishes=1)

    assert hints(outcome) == []
    assert notes(state, "stale-service") == []
    assert outcome.solved is True


@LINUX_ONLY
def test_an_edit_to_a_test_file_alone_does_not_refuse_finish(
    tmp_path: Path, machine: Machine
) -> None:
    # The stale-process question is about production code. A test file is never what a
    # running server imported, and a report even less so.
    write_app(tmp_path)
    test_file = tmp_path / "test_login.py"
    test_file.write_text("def test_login():\n    assert True\n", encoding="utf-8")
    service_pid, _ = machine.detached(tmp_path)
    wait_until_an_edit_is_newer(service_pid)
    state = codefix_state(tmp_path)
    state.baseline[str(test_file)] = "before"
    state.artifacts.append(test_file)
    test_file.write_text("def test_login():\n    assert 1 == 1\n", encoding="utf-8")

    llm = StubLlm(replies=[finish(test_file)])  # type: ignore[list-item]
    outcome = run(state, llm, FakeVerify((True, "command", "")), playbook=playbooks.FORENSICS)

    assert hints(outcome) == []
    assert notes(state, "stale-service") == []
    assert outcome.solved is True


@LINUX_ONLY
def test_an_unreadable_process_table_records_the_gap_and_refuses_nothing(
    tmp_path: Path, machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not being able to look is not evidence. The run continues exactly as it did before
    # this gate existed, and the trace says the check ran blind.
    changed = write_app(tmp_path)
    service_pid, _ = machine.detached(tmp_path)
    wait_until_an_edit_is_newer(service_pid)
    monkeypatch.setattr(tools, "PROC_ROOT", tmp_path / "no-such-proc")
    state = codefix_state(tmp_path)

    outcome, _ = run_fix(state, changed, finishes=1)

    assert hints(outcome) == []
    assert outcome.solved is True
    assert len(notes(state, "stale-service-unknown")) == 1
    assert "procfs" in notes(state, "stale-service-unknown")[0]


@LINUX_ONLY
def test_a_run_that_changed_no_production_source_pays_for_no_scan(
    tmp_path: Path, machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The common case on an audit or forensics task: nothing a process could be running
    # was touched, so the scan must not happen at all -- not even to say it found nothing.
    report = tmp_path / "report.txt"
    scans = []
    monkeypatch.setattr(
        tools,
        "process_table",
        lambda **kwargs: scans.append(kwargs) or tools.ProcessTable(),
    )
    state = make_state(tmp_path)
    llm = StubLlm(
        replies=[
            action("write_file", path=str(report), content="findings\n"),
            action("finish", path=str(report), verify="grep -q findings report.txt"),
        ]
    )

    outcome = run(state, llm, FakeVerify((True, "command", "")))

    assert outcome.solved is True
    assert scans == [], "a report is not code anything is running"
    assert notes(state, "stale-service") == [] and notes(state, "stale-service-unknown") == []


@LINUX_ONLY
def test_a_service_whose_start_time_is_unknown_records_the_gap_and_refuses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A synthetic process table, because a kernel that reports no boot time cannot be
    # arranged by starting a program. The process is found and is plainly running the
    # changed package -- but without a start time nothing can be said about whether it
    # loaded the change, and a gate that guessed here would refuse correct runs.
    changed = write_app(tmp_path)
    proc = write_proc(
        tmp_path.parent / "fake-proc", [FakeProcess(4242, ("uvicorn", "main:app"), cwd=tmp_path)]
    )
    (proc / "stat").write_text("cpu  1 2 3\nprocesses 7\n", encoding="utf-8")
    monkeypatch.setattr(tools, "PROC_ROOT", proc)
    state = codefix_state(tmp_path)

    outcome, _ = run_fix(state, changed, finishes=1)

    assert hints(outcome) == []
    assert outcome.solved is True
    assert "no start time" in "".join(notes(state, "stale-service-unknown"))


@LINUX_ONLY
def test_the_hint_names_the_service_read_from_a_synthetic_process_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same refusal as the real-subprocess test above, with every number fixed: the
    # wording, the port and both clock times are asserted exactly, which a real process
    # started microseconds ago cannot give.
    changed = write_app(tmp_path)
    proc = write_proc(
        tmp_path.parent / "fixed-proc",
        [FakeProcess(4243, ("uvicorn", "main:app"), cwd=tmp_path, inodes=(4242,))],
        listening={4242: 8000},
    )
    monkeypatch.setattr(tools, "PROC_ROOT", proc)
    state = codefix_state(tmp_path)

    outcome, _ = run_fix(state, changed, finishes=1)

    said = hints(outcome)
    assert len(said) == 1
    started = time.strftime("%H:%M:%S", time.localtime(BOOT + 10.0))
    assert f"`uvicorn main:app` (pid 4243, слушает :8000) запущен в {started}" in said[0]
    assert "раньше твоей правки routers/auth.py" in said[0]
    assert "finish" in said[0], "say what to do after the restart, not only what is wrong"
    assert notes(state, "stale-service") == [
        f"pid 4243 `uvicorn main:app` started {started} before routers/auth.py changed at "
        f"{time.strftime('%H:%M:%S', time.localtime(changed.stat().st_mtime))}; ports :8000; "
        f"loads main.py from {tmp_path.resolve()}, where auth.py changed"
    ]


# --- the restart the gate dictates must be a command the gate accepts --------
#
# The hint hands the model a ready `nohup … &` line, and the code-fix shell policy
# refused exactly that line: a lone `&` defeats the part splitter and `nohup` is an
# opaque runner. On the public set the model retyped it four times per episode for
# nothing (`build/eval-KT7-public/logs/fix-sqli-login.r2.log`, `.r3.log`). The command
# under test is never retyped here: it is cut out of the hint the code just produced.


RELAUNCH_IN_HINT = re.compile(r"Готовая команда: `(?P<command>[^`]+)`")


def offered_relaunch(hint: str) -> str:
    match = RELAUNCH_IN_HINT.search(hint)
    assert match is not None, hint
    return match["command"]


def synthetic_stale_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    argv: tuple[str, ...] = ("uvicorn", "main:app"),
) -> tuple[AgentState, str]:
    """A workspace whose edit is newer than the one process serving it, and its hint."""
    changed = write_app(tmp_path)
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / ".git").mkdir(exist_ok=True)
    proc = write_proc(
        tmp_path.parent / f"{tmp_path.name}-proc",
        [FakeProcess(4243, argv, cwd=tmp_path, inodes=(4242,))],
        listening={4242: 8000},
    )
    monkeypatch.setattr(tools, "PROC_ROOT", proc)
    state = codefix_state(tmp_path)
    state.artifacts.append(changed)
    kind, _, hint = loop._stale_service(state)
    assert kind == "stale-service", kind
    return state, hint


@LINUX_ONLY
def test_the_restart_command_the_hint_dictates_is_not_refused_by_the_shell_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, hint = synthetic_stale_service(tmp_path, monkeypatch)
    command = offered_relaunch(hint)

    assert loop._code_fix_shell_refusal(command, state) == "", command


@LINUX_ONLY
@pytest.mark.parametrize(
    "template",
    [
        "cd {cwd} && nohup {argv} >/tmp/service-restart.log 2>&1 &",
        "nohup {argv} >/tmp/service-restart.log 2>&1 &",
        "nohup {argv} > /tmp/service-restart.log 2>&1 &",
        "kill {pid} 2>/dev/null; sleep 1; cd {cwd} && nohup {argv} >/tmp/service-restart.log 2>&1 &",
        "kill {pid}; cd {cwd} && nohup {argv} >/tmp/restart.log 2>&1 &",
        "sleep 2; nohup {argv} >/tmp/service-restart.log 2>&1 &",
    ],
)
def test_the_natural_spellings_of_that_same_restart_are_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, template: str
) -> None:
    # The model does not copy the hint byte for byte: it drops the `kill` prefix once
    # the service is already down, adds a settle `sleep`, or spells the redirect with a
    # space. The argv, the pid and the destination are what the policy checks.
    state, _ = synthetic_stale_service(tmp_path, monkeypatch)
    command = template.format(cwd=tmp_path, argv="uvicorn main:app", pid=4243)

    assert loop._code_fix_shell_refusal(command, state) == "", command


@LINUX_ONLY
@pytest.mark.parametrize(
    "template",
    [
        # another program entirely, wearing the accepted shape
        "cd {cwd} && nohup python3 -m http.server 8000 >/tmp/service-restart.log 2>&1 &",
        "cd {cwd} && nohup uvicorn other:app >/tmp/service-restart.log 2>&1 &",
        # the destination is the evaluator, not /tmp
        "cd {cwd} && nohup {argv} >tests/service-restart.log 2>&1 &",
        "cd {cwd} && nohup {argv} >/tmp/../{cwd}/tests/x.log 2>&1 &",
        # somebody else's process
        "kill 999 2>/dev/null; sleep 1; cd {cwd} && nohup {argv} >/tmp/service-restart.log 2>&1 &",
        "pkill -f uvicorn; nohup {argv} >/tmp/service-restart.log 2>&1 &",
        # a second command riding along
        "cd {cwd} && nohup {argv} >/tmp/service-restart.log 2>&1 & rm -rf tests",
        "cd {cwd} && nohup {argv} >/tmp/service-restart.log 2>&1 &; rm -rf tests",
        "cd {cwd} && nohup {argv} >/tmp/service-restart.log 2>&1 & cat tests/test_a.py > /tmp/x",
        # leaving the workspace, or landing in the tree that must not be written
        "cd /etc && nohup {argv} >/tmp/service-restart.log 2>&1 &",
        "cd {cwd}/tests && nohup {argv} >/tmp/service-restart.log 2>&1 &",
        "cd {cwd}/.git && nohup {argv} >/tmp/service-restart.log 2>&1 &",
    ],
)
def test_the_restart_shape_does_not_launder_anything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, template: str
) -> None:
    state, _ = synthetic_stale_service(tmp_path, monkeypatch)
    command = template.format(cwd=tmp_path, argv="uvicorn main:app")

    assert loop._code_fix_shell_refusal(command, state) != "", command


@LINUX_ONLY
def test_no_relaunch_is_accepted_before_a_stale_service_was_ever_found(
    tmp_path: Path
) -> None:
    # The privilege comes from the scan, not from the spelling: with no hint behind it
    # the same line is an unexplained background launch and stays refused.
    write_app(tmp_path)
    state = codefix_state(tmp_path)

    command = f"cd {tmp_path} && nohup uvicorn main:app >/tmp/service-restart.log 2>&1 &"
    assert loop._code_fix_shell_refusal(command, state) != ""


@LINUX_ONLY
def test_killing_the_service_first_and_relaunching_it_second_both_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two-step path a model actually takes: stop it, then start it again.

    `kill <pid>` alone was already accepted while every background start was refused, so
    the model could take the service down and never bring it up (audit-0917 loop.md, L1).
    The relaunch is judged against the argv the scan read, which outlives the process.
    """
    state, hint = synthetic_stale_service(tmp_path, monkeypatch)
    assert loop._code_fix_shell_refusal("kill 4243", state) == ""

    # The service is gone now: its /proc entry with it.
    gone = write_proc(tmp_path.parent / f"{tmp_path.name}-gone", [])
    monkeypatch.setattr(tools, "PROC_ROOT", gone)

    command = offered_relaunch(hint).split("&& ", 1)[-1]
    assert command.startswith("nohup "), command
    assert loop._code_fix_shell_refusal(command, state) == "", command


@LINUX_ONLY
def test_the_dictated_restart_really_runs_and_reopens_the_finish_gate(
    tmp_path: Path, machine: Machine
) -> None:
    """End to end, with real processes: stop the service, run the offered line, finish.

    The three things a policy change cannot prove on its own -- that the tool layer runs
    the accepted command instead of hanging on it, that the job outlives the step, and
    that the gate which refused the finish now lets it through -- are only visible here.
    """
    changed = write_app(tmp_path)
    service_pid, port = machine.detached(tmp_path)
    wait_until_an_edit_is_newer(service_pid)
    changed.write_text("def login():\n    return 'new'\n", encoding="utf-8")
    state = codefix_state(tmp_path)
    state.artifacts.append(changed)

    kind, _, hint = loop._stale_service(state)
    assert kind == "stale-service", kind
    relaunch = offered_relaunch(hint)

    stopped = loop._execute(
        prompt.RUN_SHELL, {"command": f"kill {service_pid}"}, state, playbook=playbooks.CODE_FIX
    )
    assert stopped.ok, stopped.detail
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            os.kill(service_pid, 0)
        except OSError:
            break
        time.sleep(0.02)

    started = time.monotonic()
    restarted = loop._execute(
        prompt.RUN_SHELL, {"command": relaunch}, state, playbook=playbooks.CODE_FIX
    )
    assert restarted.ok, restarted.detail
    assert time.monotonic() - started < 10.0, "the step must not wait on the service"
    assert "перезапуск выполнен" in restarted.output, restarted.output
    assert wait_for_port(port), "the relaunched service never came up"
    for found in tools.serving_processes([changed]).processes:
        machine.pids.append(found.pid)

    assert loop._stale_service(state) == ("", "", ""), "the finish gate is still shut"
    assert state.service_restarts == [], "a restart that worked has spent its offer"


def prepare_stale(tmp_path: Path, machine: Machine, source: str) -> tuple[AgentState, str, int, int]:
    """A workspace whose edit -- `source` -- is newer than the service running it."""
    changed = write_app(tmp_path)
    service_pid, port = machine.detached(tmp_path)
    wait_until_an_edit_is_newer(service_pid)
    changed.write_text(source, encoding="utf-8")
    state = codefix_state(tmp_path)
    state.artifacts.append(changed)
    kind, _, hint = loop._stale_service(state)
    assert kind == "stale-service", kind
    Path("/tmp/service-restart.log").unlink(missing_ok=True)
    return state, offered_relaunch(hint), service_pid, port


@LINUX_ONLY
def test_a_relaunch_whose_service_dies_is_a_failed_action_and_says_why(
    tmp_path: Path, machine: Machine
) -> None:
    """A background start exits 0 and prints nothing whatever happens to the service.

    The service's own output went into the log, and the finish gate -- which asks only
    whether a process older than the edit is running -- opens once the old process is
    gone. End to end that finished a run `solved` with a dead service (critic, 17.09).
    """
    state, relaunch, service_pid, _ = prepare_stale(
        tmp_path, machine, "import nonexistent_module_xyz\ndef login():\n    return 'new'\n"
    )
    stopped = loop._execute(
        prompt.RUN_SHELL, {"command": f"kill {service_pid}"}, state, playbook=playbooks.CODE_FIX
    )
    assert stopped.ok, stopped.detail
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            os.kill(service_pid, 0)
        except OSError:
            break
        time.sleep(0.02)

    result = loop._execute(
        prompt.RUN_SHELL, {"command": relaunch}, state, playbook=playbooks.CODE_FIX
    )

    assert result.ok is False, result.output
    assert "сервис сейчас не поднят" in result.output
    assert "nonexistent_module_xyz" in result.output, "quote the log the model cannot open"
    assert state.service_restarts, "a restart that failed keeps its offer for the retry"


@LINUX_ONLY
def test_a_relaunch_the_old_process_still_blocks_names_that_process(
    tmp_path: Path, machine: Machine
) -> None:
    """`kill; sleep 1; start` is a race, and the loser is silent.

    The old process still holds the port, the new one dies on `Address already in use`,
    and the port answers -- with the old code. The way out is to stop that process and
    run the same command again, so the observation names it and the offer survives.
    """
    state, relaunch, service_pid, port = prepare_stale(
        tmp_path, machine, "def login():\n    return 'new'\n"
    )

    result = loop._execute(
        prompt.RUN_SHELL, {"command": relaunch}, state, playbook=playbooks.CODE_FIX
    )

    assert result.ok is False, result.output
    assert f"старый процесс {service_pid} ещё жив" in result.output
    assert "повтори команду перезапуска" in result.output
    assert wait_for_port(port, timeout_s=1.0), "the old service is what still answers"
    assert state.service_restarts, "the retry needs the offer"
