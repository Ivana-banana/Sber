"""Where the agent thinks the task lives decides whether it can solve anything.

Harbor execs the agent with the working directory set to the agent's own install
directory, not the task's. Treating cwd as the workspace was a real defect here; these
tests exist so it cannot come back.
"""

from __future__ import annotations

from pathlib import Path

from agent import limits
from agent.main import EXIT_OK, EXIT_USAGE, build_state, fingerprint, main, resolve_workspace

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


def test_resolution_always_lands_somewhere_real(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(limits, "IMAGE_WORKDIR", str(tmp_path / "definitely-absent"))
    workspace, _ = resolve_workspace({}, cwd=tmp_path, agent_dir=tmp_path)
    assert workspace.is_dir()


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
    main(["run.sh", instruction], {**ENV, "OPENAI_API_KEY": secret}, tmp_path)

    out = capsys.readouterr().out
    assert secret not in out
    assert instruction not in out
    assert fingerprint(instruction) in out, "the digest stands in for the text"
