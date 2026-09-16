"""The restart hint's ready command must be runnable by the model it is given to.

C4 hands the model `cd <dir> && nohup <argv> >/tmp/service-restart.log 2>&1 &`. Under the
code-fix playbook `_code_fix_shell_refusal` rejects every compound command and every
`nohup`, so the one command the hint offers was refused, the model could only `kill` the
service, and its own checks then ran against a dead port.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from agent import playbooks
from agent.ports import LlmReply, Message
from tests.stub_llm import StubLlm
from tests.test_loop import FakeVerify, action, run
from tests.test_loop_restart_hint import (
    LINUX_ONLY,
    Machine,
    codefix_state,
    finish,
    machine,  # noqa: F401 -- pytest fixture
    wait_for_port,
    wait_until_an_edit_is_newer,
    write_app,
)

READY = re.compile(r"Готовая команда: `(.+?)` —")


@dataclass
class RelaunchingLlm(StubLlm):
    """Replays the queue, but answers the refused finish with the hint's own command."""

    relaunch: str = ""
    relaunched: bool = False
    killed: int = 0

    def complete_structured(self, messages: list[Message], **kwargs: object) -> LlmReply:  # type: ignore[override]
        self.calls.append(messages)
        found = READY.search(str(messages[-1]["content"]))
        if found and not self.relaunch:
            # What the hint says, in its order: stop that pid, then the ready command.
            self.relaunch = found.group(1)
            pid = re.search(r"`kill (\d+)`", str(messages[-1]["content"]))
            assert pid, "the hint names the pid to stop"
            self.killed = int(pid.group(1))
            return _shell(f"kill {pid.group(1)}")
        if self.relaunch and not self.relaunched:
            # A model's next call takes seconds; this one takes none. Let the old
            # process release the port first, as it would have by then.
            _wait_gone(self.killed)
            self.relaunched = True
            return _shell(self.relaunch)
        reply = self.replies[self._next]
        self._next += 1
        return reply  # type: ignore[return-value]


def resize(changed: Path) -> object:
    # Not the shared `fix`: 'old' -> 'new' keeps the size, and an edit landing in the same
    # second as the original write leaves the service's .pyc valid, so a correct relaunch
    # can still serve the old code -- a flake under load, not the behaviour under test.
    return action("replace_text", path=str(changed), old="return 'old'", new="return 'renewed'")


def _wait_gone(pid: int, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.02)


def _shell(command: str) -> LlmReply:
    return LlmReply(
        text=json.dumps({"tool": "run_shell", "command": command}), input_tokens=10, output_tokens=5
    )


@LINUX_ONLY
def test_the_hinted_relaunch_command_is_not_refused_and_brings_the_port_back(
    tmp_path: Path, machine: Machine  # noqa: F811
) -> None:
    changed = write_app(tmp_path)
    service_pid, port = machine.detached(tmp_path)
    wait_until_an_edit_is_newer(service_pid)
    state = codefix_state(tmp_path)
    llm = RelaunchingLlm(replies=[resize(changed), finish(changed), finish(changed)])  # type: ignore[list-item]

    try:
        outcome = run(state, llm, FakeVerify((True, "command", "")), playbook=playbooks.CODE_FIX)
        assert llm.relaunch, "the hint carried a ready command"
        shell = [text for text in outcome.observations if "run_shell" in text]
        assert len(shell) == 2 and not any("refused" in text for text in shell), shell
        assert wait_for_port(port), "the relaunched service answers on the same port"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            assert response.read() == b"renewed", "and it serves the edited code"
    finally:
        _kill_listener(port)


def _kill_listener(port: int) -> None:
    import os
    import signal

    from agent import tools

    for info in tools.process_table(max_processes=1_000_000).processes:
        if "main.py" in " ".join(info.argv) and str(port) in info.argv:
            try:
                os.kill(info.pid, signal.SIGKILL)
            except OSError:
                pass
