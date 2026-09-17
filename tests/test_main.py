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
from agent.state import Event
from agent.main import (
    EXIT_OK,
    EXIT_USAGE,
    build_state,
    emit_phase,
    fingerprint,
    main,
    resolve_workspace,
    select_controller,
)
from agent.ports import LlmReply
from scripts.eval.run_suite import DONE_LINE
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


def test_build_state_captures_a_separate_production_source_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("answer = 1\n", encoding="utf-8")

    state = build_state(
        ["run.sh", f"Fix the source in {source}"],
        {limits.WORKSPACE_ENV_NAME: str(tmp_path)},
        tmp_path,
    )

    assert state.source_snapshot is not None
    assert state.source_snapshot.known is True
    assert state.source_snapshot.files == {"app.py": state.source_snapshot.files["app.py"]}


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
# Measured, not imagined: E011 ran the archive against a real model on packages whose
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
    (image_workdir / "main.py").write_text("app = None\n", encoding="utf-8")
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
    # If it is never populated, both fall back to workspace-only confinement and E011
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


def test_the_endpoint_credentials_are_popped_from_the_process_environment(tmp_path: Path, monkeypatch) -> None:
    # A child shell already gets a scrubbed environment, but its parent is this process;
    # `cat /proc/$PPID/environ | base64` would read the key straight out of ours, past
    # substring redaction. Once the settings and the redaction set are built, the three
    # variables are popped from os.environ before the first shell can run. Guarded on
    # identity, so a caller passing its own env mapping is never mutated.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-canary-0123456789")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("LOCAL_AGENT_MODEL", "small-local")

    assert main(["run.sh", "task"], os.environ, tmp_path, llm=StubLlm(replies=[])) == EXIT_OK

    assert "OPENAI_API_KEY" not in os.environ
    assert "OPENAI_BASE_URL" not in os.environ
    assert "LOCAL_AGENT_MODEL" not in os.environ


def test_a_caller_supplied_env_mapping_is_not_mutated(tmp_path: Path) -> None:
    # The pop is guarded on `env is os.environ`; a test or embedder passing its own dict
    # keeps every key it started with.
    env = dict(ENV)
    main(["run.sh", "task"], env, tmp_path, llm=StubLlm(replies=[]))
    assert env == dict(ENV)


def test_an_uncaught_exception_is_a_crash_not_a_traceback(tmp_path: Path, capsys, monkeypatch) -> None:
    # An exception escaping the run (a NUL or over-long path out of `Path.resolve`, a
    # bug anywhere in preflight or the controller) must not become exit 1 with a
    # traceback: that is a hard zero and it dumps internal paths. It reports stop=crash
    # and exits 0, and the printed line carries no exception message.
    def boom(*args: object, **kwargs: object) -> object:
        raise ValueError("secret-ish detail /opt/harbor/should-not-appear")

    monkeypatch.setattr("agent.main.build_state", boom)

    assert main(["run.sh", "task"], ENV, tmp_path) == EXIT_OK
    out = capsys.readouterr().out
    assert "stop=crash" in out
    assert "should-not-appear" not in out, "the exception message must not leak"


def test_the_evaluator_can_parse_the_crash_line(tmp_path: Path, capsys, monkeypatch) -> None:
    # The crash line is the only record a crashed run leaves. `run_suite` reads a run
    # through `DONE_LINE`; a line it cannot parse leaves `stop_reason` at its default,
    # and the row is filed as an ordinary reasoning zero with tokens unknown. That hides
    # a defect in our own code inside the one taxonomy we use to decide what to fix.
    def boom(*args: object, **kwargs: object) -> object:
        raise ValueError("secret-ish detail /opt/harbor/should-not-appear")

    monkeypatch.setattr("agent.main.build_state", boom)

    assert main(["run.sh", "task"], ENV, tmp_path) == EXIT_OK
    out = capsys.readouterr().out

    done = DONE_LINE.search(out)
    assert done is not None, "run_suite cannot parse the crash line"
    assert done.group("stop") == "crash"
    assert done.group("solved") == "False"
    assert "ValueError" in out, "the exception class is the only diagnostic left"
    assert "should-not-appear" not in out, "the exception message must not leak"


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


def test_phase_trace_times_inspect_plan_act_and_verify_without_task_text(
    tmp_path: Path, capsys
) -> None:
    """A timeout log must identify its in-flight phase without exposing input data."""
    instruction = "private instruction that must not enter diagnostics"
    target = tmp_path / "answer.txt"
    llm = StubLlm(
        replies=[
            LlmReply(text=json.dumps({"tool": "write_file", "path": str(target), "content": "A"})),
            LlmReply(text=json.dumps({"tool": "finish", "path": str(target)})),
        ]
    )

    assert main(["run.sh", instruction], ENV, tmp_path, llm=llm) == EXIT_OK

    out = capsys.readouterr().out
    phase_lines = [line for line in out.splitlines() if line.startswith("agent phase:")]
    expected = [
        "phase=inspect status=start",
        "phase=inspect status=end",
        "phase=plan status=start",
        "phase=plan status=end",
        "phase=act status=start",
        "phase=act status=end",
        "phase=verify status=start",
        "phase=verify status=end",
    ]
    positions = [next(i for i, line in enumerate(phase_lines) if marker in line) for marker in expected]

    assert positions == sorted(positions), phase_lines
    assert all("step=" in line and "duration_s=" in line for line in phase_lines)
    assert instruction not in out


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

    E011 measured the alternative: stdout is a pipe, Python buffers it in blocks, the
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
    # Read on a thread with a deadline. Buffered diagnostics do not arrive late, they
    # do not arrive at all until the process exits -- so a plain readline() here would
    # hang the suite instead of failing it. The plan-start marker is the crucial one:
    # an outer kill during generation has no matching end marker, and that is the
    # evidence that generation, rather than a tool or verifier, held the deadline.
    live_lines: list[str] = []

    def read_until_plan() -> None:
        assert process.stdout is not None
        for _ in range(8):
            line = process.stdout.readline()
            live_lines.append(line)
            if "phase=plan status=start" in line:
                return

    reader = threading.Thread(target=read_until_plan, daemon=True)
    reader.start()
    reader.join(timeout=20.0)

    try:
        assert live_lines, "no status line arrived while the run was alive: stdout is buffered"
        assert live_lines[0].startswith("agent ready:")
        assert any("phase=plan status=start" in line for line in live_lines), live_lines
    finally:
        process.kill()
        process.wait(timeout=30)
        for conn in accepted:
            conn.close()
        listener.close()


def test_a_ladder_demotion_is_printed_and_not_only_recorded(capsys) -> None:
    """A demotion is the difference between one request per step and two.

    Recorded into the audit and printed nowhere, it made E037's refutation criterion
    unreadable: no artefact of any run could show whether the rung was still being
    paid for.
    """
    emit_phase(Event(step=1, kind="degrade", name="llm", detail="schema: x -> call style json_schema"))

    assert capsys.readouterr().out == (
        "agent degrade: step=1 schema: x -> call style json_schema\n"
    )


def test_a_protocol_note_is_bounded_because_the_endpoint_wrote_part_of_it(capsys) -> None:
    emit_phase(Event(step=2, kind="protocol", name="llm", detail="e" * 400))

    printed = capsys.readouterr().out
    assert printed == f"agent protocol: step=2 {'e' * 120}\n"


def test_events_that_are_neither_timing_nor_protocol_stay_out_of_the_log(capsys) -> None:
    """`retry` and the tool audit are already visible in the phase line's duration."""
    emit_phase(Event(step=3, kind="retry", name="llm", detail="attempt 1 failed"))

    assert capsys.readouterr().out == ""


def test_an_archive_with_no_variable_set_still_runs_the_loop() -> None:
    """Every experiment before the macro comparison measured this path; unset keeps it."""
    from agent import loop, macro

    assert select_controller({}) is loop
    assert select_controller({limits.CONTROLLER_ENV_NAME: "macro"}) is macro
    assert select_controller({limits.CONTROLLER_ENV_NAME: " MACRO "}) is macro


def test_an_unrecognised_controller_name_costs_a_comparison_and_not_a_task() -> None:
    from agent import loop

    assert select_controller({limits.CONTROLLER_ENV_NAME: "typo"}) is loop
    assert select_controller({limits.CONTROLLER_ENV_NAME: ""}) is loop


def test_the_status_line_names_the_controller_that_ran(tmp_path: Path, capsys) -> None:
    """A run whose log does not say which arm it was cannot be read back afterwards."""
    main(["run.sh", "do a thing"], {}, tmp_path)

    assert "controller=loop" in capsys.readouterr().out


def test_a_base_image_workdir_with_only_hidden_entries_yields_to_the_task_text(
    tmp_path, monkeypatch
) -> None:
    # `secureintelligent/acp` ships `/app` holding nothing but `.venv`, so it exists in
    # every image built from it. A task living elsewhere then got `/app` as workspace:
    # the brief described the wrong tree, and code-fix refused every finish with "has
    # not changed a production source" after a correct edit outside it.
    image_workdir = tmp_path / "app"
    (image_workdir / ".venv").mkdir(parents=True)
    task = tmp_path / "srv" / "portal"
    task.mkdir(parents=True)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(image_workdir))

    workspace, why = resolve_workspace(
        {}, cwd=agent_dir, agent_dir=agent_dir,
        instruction=f"Repair the lookup in {task}/session_store.py.",
    )

    assert workspace == task
    assert "named by the task text" in why


def test_a_base_image_workdir_stays_when_the_task_names_nothing_else(tmp_path, monkeypatch) -> None:
    image_workdir = tmp_path / "app"
    (image_workdir / ".venv").mkdir(parents=True)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(image_workdir))

    workspace, _ = resolve_workspace({}, cwd=agent_dir, agent_dir=agent_dir, instruction="Find the flag.")

    assert workspace == image_workdir
