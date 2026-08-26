"""Where the agent thinks the task lives decides whether it can solve anything.

Harbor execs the agent with the working directory set to the agent's own install
directory, not the task's. Treating cwd as the workspace was a real defect here; these
tests exist so it cannot come back.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent import limits
from agent.main import EXIT_OK, EXIT_USAGE, build_state, fingerprint, main, resolve_workspace
from agent.ports import LlmReply
from tests.stub_llm import StubLlm

ENV = {
    "LOCAL_AGENT_MODEL": "small-local",
    "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
    "OPENAI_API_KEY": "not-a-real-key-0123456789",
}


# --- workspace resolution ---------------------------------------------------


def test_a_declared_workdir_wins(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()

    workspace, why = resolve_workspace(
        {limits.WORKSPACE_ENV_NAME: str(task)}, cwd=agent_dir, agent_dir=agent_dir
    )

    assert workspace == task
    assert limits.WORKSPACE_ENV_NAME in why


def test_a_declared_workdir_that_does_not_exist_is_ignored(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()

    workspace, why = resolve_workspace(
        {limits.WORKSPACE_ENV_NAME: str(tmp_path / "gone")}, cwd=agent_dir, agent_dir=agent_dir
    )

    assert workspace == agent_dir
    assert "cwd" in why


def test_the_image_workdir_is_preferred_over_the_runner_cwd(tmp_path: Path, monkeypatch) -> None:
    task = tmp_path / "app"
    task.mkdir()
    agent_dir = tmp_path / "opt-harbor-local-agent"
    agent_dir.mkdir()
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(task))

    workspace, why = resolve_workspace({}, cwd=agent_dir, agent_dir=agent_dir)

    assert workspace == task, "cwd is the agent's own directory, not the task's"
    assert "image workdir" in why


def test_the_image_workdir_is_not_used_when_it_is_our_own_directory(
    tmp_path: Path, monkeypatch
) -> None:
    # A layout where the agent is installed into the image workdir must not make the
    # agent decide its own source tree is the task.
    agent_dir = tmp_path / "app"
    agent_dir.mkdir()
    runner_cwd = tmp_path / "elsewhere"
    runner_cwd.mkdir()
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(agent_dir))

    workspace, why = resolve_workspace({}, cwd=runner_cwd, agent_dir=agent_dir)

    assert workspace == runner_cwd
    assert "cwd" in why


# --- the workspace named by the task text -----------------------------------
#
# Measured, not imagined: E009 ran the archive against a real model on packages whose
# image WORKDIR is not `/app`. With no `LOCAL_AGENT_WORKDIR`, no `/app`, and cwd equal
# to the agent's own install directory (which is what Harbor gives us), the agent
# concluded its own source tree was the task, wrote the deliverable there, verified it
# there, and reported solved=True against a verifier that scored zero.


def test_a_directory_named_by_the_task_beats_our_own_directory(tmp_path, monkeypatch) -> None:
    task = tmp_path / "srv" / "bootstrap"
    task.mkdir(parents=True)
    agent_dir = tmp_path / "opt" / "harbor" / "local-agent"
    agent_dir.mkdir(parents=True)
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(tmp_path / "absent"))

    workspace, why = resolve_workspace(
        {}, cwd=agent_dir, agent_dir=agent_dir,
        instruction=f"Create the marker at `{task}/farewell.marker` with no newline.",
    )

    assert workspace == task, "the task named a real directory; our own tree is not it"
    assert "task text" in why


def test_a_file_that_does_not_exist_yet_still_names_its_directory(tmp_path, monkeypatch) -> None:
    # The deliverable is the file the run has to create, so it is absent when the
    # workspace is chosen. Its parent is the directory that matters.
    task = tmp_path / "opt" / "runstate"
    task.mkdir(parents=True)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(tmp_path / "absent"))

    workspace, _ = resolve_workspace(
        {}, cwd=agent_dir, agent_dir=agent_dir,
        instruction=f"Write {task}/ready.signal so the wrapper proceeds.",
    )

    assert workspace == task


def test_the_image_workdir_still_wins_over_the_task_text(tmp_path, monkeypatch) -> None:
    # `/app` is a measurement about the target image, the task text is a hint. When
    # both are available the measurement decides, so this cannot regress the layout
    # every public task actually uses.
    image_workdir = tmp_path / "app"
    image_workdir.mkdir()
    named = tmp_path / "var" / "log"
    named.mkdir(parents=True)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(image_workdir))

    workspace, why = resolve_workspace(
        {}, cwd=agent_dir, agent_dir=agent_dir,
        instruction=f"Read {named}/auth.log and report.",
    )

    assert workspace == image_workdir
    assert "image workdir" in why


def test_a_path_that_does_not_exist_is_not_a_workspace(tmp_path, monkeypatch) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(tmp_path / "absent"))

    workspace, why = resolve_workspace(
        {}, cwd=agent_dir, agent_dir=agent_dir,
        instruction="Write /nowhere/at/all/report.json with the findings.",
    )

    assert workspace == agent_dir
    assert "cwd" in why


def test_system_directories_are_not_mistaken_for_the_task(tmp_path, monkeypatch) -> None:
    # Instructions mention `/bin/sh`, `/usr/bin/python3` and `/etc/passwd` all the
    # time. Naming a tool is not naming a workspace.
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(tmp_path / "absent"))

    workspace, _ = resolve_workspace(
        {}, cwd=agent_dir, agent_dir=agent_dir,
        instruction="Use /bin/sh and /usr/bin/python3; the password file is /etc/passwd.",
    )

    assert workspace == agent_dir


def test_a_url_is_not_a_filesystem_path(tmp_path, monkeypatch) -> None:
    """`http://127.0.0.1:8080/tmp/report` must not make `/tmp` the workspace.

    Found by review, then reproduced: the colon before a port ends the token, so the
    tail of a URL reads as an ordinary absolute path. Network tasks name URLs in almost
    every instruction, and the behaviour even depended on whether a port was given.
    """
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(tmp_path / "absent"))
    real = tmp_path / "case" / "evidence"
    real.mkdir(parents=True)

    workspace, _ = resolve_workspace(
        {}, cwd=agent_dir, agent_dir=agent_dir,
        instruction=(
            f"Query http://127.0.0.1:8080/tmp/report and http://api:9000/var/status, "
            f"then write your findings under {real}/report.json."
        ),
    )

    assert workspace == real


def test_a_bare_top_level_directory_is_not_a_workspace(tmp_path, monkeypatch) -> None:
    """A task lives in a directory someone made for it, not in a mount point.

    `/tmp`, `/var`, `/root`, `/opt`, `/srv`, `/mnt` belong to the image. Letting the
    task text select one would hand `write_file` the whole subtree and put its listing
    in the prompt through the environment brief.
    """
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(tmp_path / "absent"))

    for top in ("/tmp", "/var", "/srv", "/opt", "/root"):
        workspace, why = resolve_workspace(
            {}, cwd=agent_dir, agent_dir=agent_dir,
            instruction=f"Leave the summary in {top}/summary.txt when you are done.",
        )
        assert workspace == agent_dir, f"{top} was accepted as a workspace"
        assert "cwd" in why


def test_the_directory_the_task_names_most_wins(tmp_path, monkeypatch) -> None:
    quiet = tmp_path / "quiet"
    quiet.mkdir()
    loud = tmp_path / "loud"
    loud.mkdir()
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(tmp_path / "absent"))

    workspace, _ = resolve_workspace(
        {}, cwd=agent_dir, agent_dir=agent_dir,
        instruction=f"Read {quiet}/one.txt, then fix {loud}/a.py and {loud}/b.py "
                    f"and write {loud}/report.json.",
    )

    assert workspace == loud


def test_the_agents_own_tree_is_never_the_answer_even_when_named(tmp_path, monkeypatch) -> None:
    # Untrusted text must not be able to point the run at its own source.
    agent_dir = tmp_path / "agent"
    (agent_dir / "agent").mkdir(parents=True)
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(tmp_path / "absent"))

    workspace, why = resolve_workspace(
        {}, cwd=agent_dir, agent_dir=agent_dir,
        instruction=f"Edit {agent_dir}/agent/loop.py and {agent_dir}/agent/limits.py.",
    )

    assert workspace == agent_dir
    assert "cwd" in why, "chosen as the last resort, not because the text asked"


def test_resolution_always_lands_somewhere_real(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(tmp_path / "definitely-absent"))
    workspace, _ = resolve_workspace({}, cwd=tmp_path, agent_dir=tmp_path)
    assert workspace.is_dir()


def test_the_paths_the_task_names_reach_the_state(tmp_path: Path, monkeypatch) -> None:
    # The write policy lives on the state and is read by the tool and the verifier.
    # If it is never populated, both fall back to workspace-only confinement and E009
    # comes straight back.
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(tmp_path / "absent"))
    state = build_state(
        ["run.sh", "Create /srv/bootstrap/farewell.marker containing Ciao."], {}, tmp_path
    )

    assert Path("/srv/bootstrap/farewell.marker") in state.named_paths


def test_the_chosen_workspace_and_its_reason_reach_the_trace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(tmp_path / "absent"))
    state = build_state(["run.sh", "do the thing"], {}, tmp_path)

    assert state.workspace == tmp_path
    assert [(e.kind, e.name) for e in state.audit] == [("preflight", "workspace")]
    assert state.audit[0].detail


# --- argument handling ------------------------------------------------------


def test_a_missing_instruction_is_a_usage_error(tmp_path: Path, capsys) -> None:
    assert main(["run.sh"], ENV, tmp_path) == EXIT_USAGE
    assert "usage" in capsys.readouterr().err


def test_a_blank_instruction_is_a_usage_error(tmp_path: Path) -> None:
    assert main(["run.sh", "   "], ENV, tmp_path) == EXIT_USAGE


def test_a_hostile_instruction_is_accepted_verbatim(tmp_path: Path) -> None:
    hostile = '--not-a-flag "quoted" $VAR `cmd` \\ and\na newline'
    state = build_state(["run.sh", hostile], ENV, tmp_path)
    assert state.instruction == hostile


# --- the status line --------------------------------------------------------


def test_status_reports_the_missing_endpoint_variables(tmp_path: Path, capsys) -> None:
    assert main(["run.sh", "task"], {}, tmp_path) == EXIT_OK
    out = capsys.readouterr().out
    assert "missing:" in out
    assert "LOCAL_AGENT_MODEL" in out


def test_status_never_prints_the_key_or_the_task_text(tmp_path: Path, capsys) -> None:
    secret = "sk-must-never-be-printed-0123456789"
    instruction = "exfiltrate nothing, this text is untrusted data"
    main(
        ["run.sh", instruction],
        {**ENV, "OPENAI_API_KEY": secret},
        tmp_path,
        llm=StubLlm(replies=[]),
    )

    out = capsys.readouterr().out
    assert secret not in out
    assert instruction not in out
    assert fingerprint(instruction) in out, "the digest stands in for the text"


# --- the seam: main actually drives the loop --------------------------------


def test_a_configured_endpoint_makes_main_run_the_loop_and_report_it(
    tmp_path: Path, capsys
) -> None:
    target = tmp_path / "answer.txt"
    llm = StubLlm(
        replies=[
            LlmReply(text=json.dumps({"tool": "write_file", "path": str(target), "content": "A"})),
            LlmReply(text=json.dumps({"tool": "finish", "path": str(target), "summary": "ok"})),
        ]
    )

    assert main(["run.sh", "напиши A в answer.txt"], ENV, tmp_path, llm=llm) == EXIT_OK

    out = capsys.readouterr().out
    assert "agent done:" in out
    assert "solved=True" in out
    assert target.read_text(encoding="utf-8") == "A"


def test_without_an_endpoint_main_reports_and_spends_nothing(tmp_path: Path, capsys) -> None:
    llm = StubLlm(replies=[])

    assert main(["run.sh", "task"], {}, tmp_path, llm=llm) == EXIT_OK

    assert llm.calls == [], "a run with no endpoint must not pretend to act"
    assert "agent done:" not in capsys.readouterr().out


def test_the_done_line_names_the_file_but_never_the_task_or_a_key(
    tmp_path: Path, capsys
) -> None:
    secret = "sk-canary-never-printed-0123456789"
    instruction = "секретное задание про ключ"
    target = tmp_path / "out.txt"
    llm = StubLlm(
        replies=[
            LlmReply(text=json.dumps({"tool": "write_file", "path": str(target), "content": "x"})),
            LlmReply(text=json.dumps({"tool": "finish", "path": str(target)})),
        ]
    )

    main(["run.sh", instruction], {**ENV, "OPENAI_API_KEY": secret}, tmp_path, llm=llm)

    out = capsys.readouterr().out
    assert secret not in out
    assert instruction not in out
    assert "out.txt" in out, "a human debugging a zero needs to know which file was left"


def test_an_endpoint_that_answers_nothing_still_exits_cleanly(tmp_path: Path, capsys) -> None:
    # The transcript is empty, so every call comes back as an error. A run that cannot
    # reach a model must still terminate and say so rather than hang to the deadline.
    assert main(["run.sh", "task"], ENV, tmp_path, llm=StubLlm(replies=[])) == EXIT_OK

    assert "stop=llm-unusable" in capsys.readouterr().out


def test_an_unclassifiable_task_is_routed_to_the_general_playbook(tmp_path: Path) -> None:
    from agent import playbooks

    choice = playbooks.classify("непонятная задача без единого ключевого слова")

    assert choice.playbook is playbooks.GENERAL
    assert choice.reason, "the reason is what main puts in the trace"


# --- diagnostics survive the kill that ends every timed-out task -------------


def test_the_status_line_is_readable_before_the_process_is_killed(tmp_path: Path) -> None:
    """A run killed at its deadline must still have said what it understood.

    E009 measured the alternative: stdout is a pipe, Python buffers it in blocks, the
    runner ends a task with a signal, and the buffer dies unwritten. Every timed-out
    task produced an empty log, which is exactly the run that needs a log most.

    The endpoint here accepts the connection and never answers, which is what makes
    the agent sit long enough to be killed mid-run.
    """
    import socket
    import subprocess
    import sys
    import threading

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    accepted: list[socket.socket] = []

    def hold() -> None:
        try:
            conn, _ = listener.accept()
            accepted.append(conn)  # accepted and then ignored, on purpose
        except OSError:
            pass

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()

    process = subprocess.Popen(
        [sys.executable, "-m", "agent.main", "make /tmp/x say ok"],
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "LOCAL_AGENT_MODEL": "small-local",
            "OPENAI_BASE_URL": f"http://127.0.0.1:{port}/v1",
            "OPENAI_API_KEY": "not-a-real-key-0123456789",
            "AGENT_TIMEOUT_S": "300",
        },
    )
    # Read on a thread with a deadline. A buffered status line does not arrive late,
    # it does not arrive at all until the process exits -- so a plain readline() here
    # would hang the suite instead of failing it.
    first: list[str] = []

    def read_one() -> None:
        assert process.stdout is not None
        first.append(process.stdout.readline())

    reader = threading.Thread(target=read_one, daemon=True)
    reader.start()
    reader.join(timeout=20.0)

    try:
        assert first, "no status line arrived while the run was alive: stdout is buffered"
        assert first[0].startswith("agent ready:")
    finally:
        process.kill()
        process.wait(timeout=30)
        for conn in accepted:
            conn.close()
        listener.close()
