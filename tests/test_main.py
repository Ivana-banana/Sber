"""Where the agent thinks the task lives decides whether it can solve anything.

Harbor execs the agent with the working directory set to the agent's own install
directory, not the task's. Treating cwd as the workspace was a real defect here; these
tests exist so it cannot come back.
"""

from __future__ import annotations

import json
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
