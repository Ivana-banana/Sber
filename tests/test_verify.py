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


# --- a deliverable the task named but the write policy refuses ---------------


def _named_system_deliverable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """`tmp_path/etc/...`: a path the task names that lies in an OS tree."""
    from agent import paths

    monkeypatch.setattr(paths, "SYSTEM_TREES", (str(tmp_path / "etc"),))
    conf = tmp_path / "etc" / "nginx" / "app.conf"
    conf.parent.mkdir(parents=True)
    return conf


def test_a_deliverable_the_task_named_is_checked_even_inside_a_system_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Otherwise `finish` is refused forever and the run cannot end on its own answer.

    `run_shell` is not confined, so a hardening task's config really does get fixed --
    and then every `finish` came back "outside this run's write policy, not checked".
    This does not widen the write policy: that lives in `agent.tools`, and a tool write
    here is still refused. It only lets the run finish on a file the task called the
    deliverable.
    """
    workspace = tmp_path / "app"
    workspace.mkdir()
    conf = _named_system_deliverable(monkeypatch, tmp_path)
    conf.write_text("allow 127.0.0.1;\n", encoding="utf-8")
    state = make_state(workspace)
    state.named_paths = (conf,)

    result = verify(state, deliverable=conf, command="")

    assert result.stage != "refused"
    assert result.ok is True
    assert result.stage == "unverified"


def test_a_named_system_deliverable_that_the_run_never_changed_is_still_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The protection that makes the relaxation above safe: pointing `finish` at a file
    # that was already there, byte for byte, is not an answer.
    from agent.paths import snapshot

    workspace = tmp_path / "app"
    workspace.mkdir()
    conf = _named_system_deliverable(monkeypatch, tmp_path)
    conf.write_text("allow all;\n", encoding="utf-8")
    state = make_state(workspace)
    state.named_paths = (conf,)
    state.baseline = snapshot((conf,))

    result = verify(state, deliverable=conf, command="")

    assert result.ok is False
    assert result.stage == "stale"


def test_a_system_path_the_task_never_named_is_still_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    conf = _named_system_deliverable(monkeypatch, tmp_path)
    conf.write_text("allow all;\n", encoding="utf-8")
    state = make_state(workspace)

    result = verify(state, deliverable=conf, command="")

    assert result.ok is False
    assert result.stage == "refused"


def test_the_agents_own_archive_is_never_a_deliverable_even_if_the_task_names_it(
    tmp_path: Path,
) -> None:
    # `named_paths` is read out of untrusted task text, so "the task named it" cannot
    # be the whole rule.
    from agent import paths

    workspace = tmp_path / "app"
    workspace.mkdir()
    inside = paths.AGENT_ROOT / "agent" / "verify.py"
    state = make_state(workspace)
    state.named_paths = (inside,)

    result = verify(state, deliverable=inside, command="")

    assert result.ok is False
    assert result.stage == "refused"
