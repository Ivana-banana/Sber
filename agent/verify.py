"""Whether the claimed deliverable is real, decided by the environment, not the model.

`docs/research/verifier-contract.md` looked at all six public task verifiers and
found the same shape every time: each reads one absolute path or one HTTP response,
none of them reads agent stdout, and each is binary on that one signal. `finish` in
`agent/prompt.py` asks the model for a `path` and a `verify` command; this module is
what actually decides whether that claim holds, by touching the same two things a real
verifier would -- the file, and the exit code of a command run against it.

Four outcomes, in the order they are checked:

* `existence`  -- the path is not a file at all. Every public verifier starts here.
* `non-empty`  -- the file is there but empty; no public verifier accepts that either.
* `unverified` -- the model gave no verify command. Not a failure: refusing to finish
  over a missing self-check would be inventing a rule the real verifier does not have
  (M13, `docs/research/verdict-verifier-contract.md`) -- it reads the environment
  regardless of what runs here. `ok=True` so the run can still finish and leave the
  reserve for something that needs it more.
* `command`    -- the model's own command ran. Its exit code decides, not its stdout;
  `fix-sqli-login`'s reference check is `[ "$(cat bye.txt)" = "Bye" ]`-shaped, and a
  command that echoes success while exiting non-zero is exactly the case a real
  verifier would also fail.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent.paths import permits
from agent.redact import redact
from agent.state import AgentState
from agent.tools import run_shell

STAGE_EXISTENCE = "existence"
STAGE_REFUSED = "refused"
STAGE_STALE = "stale"
STAGE_NON_EMPTY = "non-empty"
STAGE_COMMAND = "command"
STAGE_UNVERIFIED = "unverified"

STALE_GRACE_S = 2.0
"""How far before the run's start a deliverable's mtime may sit and still count.

Not zero: a filesystem's mtime resolution, a container clock and the moment the state
object is built are all slightly apart, and a two-second grace costs nothing while a
strict comparison would fail a file the run really did write."""


@dataclass(frozen=True)
class VerifyResult:
    """One verification outcome. `detail` is one line, meant to reach the model."""

    ok: bool
    stage: str
    detail: str


def _resolve_deliverable(
    path: Path, workspace: Path, permitted: tuple[Path, ...] = ()
) -> Path | None:
    """Join a workspace-relative path; refuse one the run was never allowed to write.

    Mirrors `agent.tools.write_file`'s own guard. `finish(path, ...)` takes whatever
    path shape the model gives it, and the model has no reliable way to know whether
    the loop's cwd equals the workspace -- `agent/main.py`'s own `resolve_workspace`
    exists because that assumption was already wrong once. `None` on escape rather
    than raising, because a wrong or hostile path is data for `verify` to report, not
    a reason to crash the run.
    """
    try:
        root = workspace.resolve()
        target = path if path.is_absolute() else root / path
        target = target.resolve()
    except OSError:
        return None
    if not permits(target, root, permitted):
        return None
    return target


def verify(state: AgentState, *, deliverable: Path, command: str) -> VerifyResult:
    """Check a claimed deliverable the way a public verifier would.

    `deliverable` may be relative to `state.workspace`, since that is what `finish`
    hands the loop; it is resolved and confined the same way `write_file` confines a
    write -- inside the workspace, or at a path the task itself named. The two have to
    agree: a deliverable the run was allowed to write but not allowed to check would
    fail verification for existing in the place the task asked for.

    The command's timeout comes from `state.budget.tool_timeout_s()`, never a tool
    default: a verify command is acting, and acting past the reserve is what
    `Budget.may_recover()` exists to prevent one step later.

    Raw command output never reaches `detail` -- only its exit code and the tool's own
    short failure label do. The command text itself is redacted before it is echoed
    back, because the text came from the model and can embed a secret the same way
    output can (`tests/test_verify.py` caught this by putting the canary in the
    command, not the output).
    """
    resolved = _resolve_deliverable(deliverable, state.workspace, state.named_paths)
    if resolved is None:
        # Deliberately not "not found". Review caught the old message telling the model
        # a file it had just created did not exist, because both cases collapsed into
        # one string -- and `run_shell` is not confined, so creating the artifact at the
        # right absolute path is something a run can really do. Being told the truth is
        # what lets it correct the path instead of moving the file somewhere wrong.
        return VerifyResult(
            ok=False,
            stage=STAGE_REFUSED,
            detail=f"outside this run's write policy, not checked: {deliverable}",
        )
    if not resolved.is_file():
        return VerifyResult(
            ok=False,
            stage=STAGE_EXISTENCE,
            detail=f"expected deliverable not found: {deliverable}",
        )
    deliverable = resolved

    try:
        modified = deliverable.stat().st_mtime
    except OSError as exc:
        return VerifyResult(
            ok=False, stage=STAGE_EXISTENCE, detail=f"{deliverable}: {exc.__class__.__name__}"
        )
    if modified < state.started_wall - STALE_GRACE_S:
        # The task's own input file is a file that exists and is non-empty, so without
        # this the cheapest way to "solve" anything is to point `finish` at something
        # that was already there. Widening the write policy to paths the task names
        # made that cheaper still: any existing file the text mentions would do.
        return VerifyResult(
            ok=False,
            stage=STAGE_STALE,
            detail=f"{deliverable} predates this run; it is not something this run produced",
        )

    if deliverable.stat().st_size == 0:
        return VerifyResult(
            ok=False,
            stage=STAGE_NON_EMPTY,
            detail=f"{deliverable} exists but is empty",
        )

    stripped = command.strip()
    if not stripped:
        return VerifyResult(
            ok=True,
            stage=STAGE_UNVERIFIED,
            detail=f"{deliverable} exists and is non-empty; no verify command was given",
        )

    result = run_shell(
        stripped,
        cwd=state.workspace,
        timeout_s=state.budget.tool_timeout_s(),
        secrets=state.secrets,
    )
    safe_command = redact(stripped, state.secrets)
    if result.ok:
        return VerifyResult(ok=True, stage=STAGE_COMMAND, detail=f"verify command exited 0: {safe_command}")

    reason = result.detail or f"exit {result.exit_code}"
    return VerifyResult(
        ok=False,
        stage=STAGE_COMMAND,
        detail=f"verify command failed ({reason}): {safe_command}",
    )
