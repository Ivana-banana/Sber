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
  an exact-output shell comparison is the representative shape, and a command that
  echoes success while exiting non-zero is exactly the case a real verifier would
  also fail.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent import limits, result_contract
from agent.paths import digest, permits
from agent.redact import redact
from agent.state import AgentState
from agent.tools import run_shell

STAGE_EXISTENCE = "existence"
STAGE_REFUSED = "refused"
STAGE_STALE = "stale"
STAGE_NON_EMPTY = "non-empty"
STAGE_COMMAND = "command"
STAGE_UNVERIFIED = "unverified"
STAGE_EXACT_FILE = "exact-file"


def _single_line(value: str) -> str:
    """Keep verifier details on one physical line before telemetry renders them."""
    escaped: list[str] = []
    for char in value:
        code = ord(char)
        if char == "\n":
            escaped.append(r"\n")
        elif char == "\r":
            escaped.append(r"\r")
        elif char == "\t":
            escaped.append(r"\t")
        elif code < 32 or code == 127:
            escaped.append(f"\\x{code:02x}")
        else:
            escaped.append(char)
    return "".join(escaped)


def _untouched(state: AgentState, deliverable: Path) -> bool:
    """Is this exactly the file that was already there when the run began?

    Positive evidence only. A file the run wrote through `write_file` is known to be
    its own work; a file whose bytes match the snapshot taken at startup is known not
    to be. Anything else -- a path nobody snapshotted, a file created by `run_shell` --
    is left alone rather than judged on a guess, because the direction of a wrong
    answer here is a correct deliverable rejected.
    """
    if any(artifact.resolve() == deliverable for artifact in state.artifacts):
        return False
    before = state.baseline.get(str(deliverable))
    return bool(before) and before == digest(deliverable)


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

    Failed command output is relayed in a smaller bounded excerpt: an assertion or
    missing binary is useful recovery evidence, while a whole test suite is not. The
    output is already bounded by `run_shell` and is redacted again here before it is
    appended. The command text itself is redacted before it is echoed back, because
    the text came from the model and can embed a secret the same way output can.
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

    exact = result_contract.exact_file_requirement(state.instruction)
    if exact is not None:
        expected_path, expected_bytes = exact
        try:
            expected_path = expected_path.resolve()
        except OSError:
            return VerifyResult(
                ok=False,
                stage=STAGE_EXACT_FILE,
                detail="exact-file requirement has an unusable target path",
            )
        if deliverable != expected_path:
            return VerifyResult(
                ok=False,
                stage=STAGE_EXACT_FILE,
                detail=f"exact-file requirement expects {expected_path}, not {deliverable}",
            )
        try:
            with deliverable.open("rb") as handle:
                actual = handle.read(len(expected_bytes) + 1)
        except OSError as exc:
            return VerifyResult(
                ok=False,
                stage=STAGE_EXACT_FILE,
                detail=f"exact-file requirement could not read the file: {type(exc).__name__}",
            )
        if actual != expected_bytes:
            return VerifyResult(
                ok=False,
                stage=STAGE_EXACT_FILE,
                detail=(
                    f"exact-file requirement failed for {deliverable}: expected "
                    f"{len(expected_bytes)} UTF-8 bytes"
                ),
            )
        return VerifyResult(
            ok=True,
            stage=STAGE_EXACT_FILE,
            detail=f"exact-file requirement passed for {deliverable}",
        )

    if deliverable.stat().st_size == 0:
        return VerifyResult(
            ok=False,
            stage=STAGE_NON_EMPTY,
            detail=f"{deliverable} exists but is empty",
        )

    if _untouched(state, deliverable):
        # The task's own input file exists and is non-empty, so without this the
        # cheapest way to "solve" anything is to point `finish` at something that was
        # already there -- and widening the write policy to the paths the task names
        # made that cheaper still. Checked after `non-empty` so an empty stale file is
        # still diagnosed as empty, which is the more useful thing to be told.
        return VerifyResult(
            ok=False,
            stage=STAGE_STALE,
            detail=f"{deliverable} is byte-for-byte what it was before this run started",
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
    safe_command = _single_line(redact(stripped, state.secrets))
    if result.ok:
        return VerifyResult(ok=True, stage=STAGE_COMMAND, detail=f"verify command exited 0: {safe_command}")

    reason = _single_line(result.detail or f"exit {result.exit_code}")
    detail = f"verify command failed ({reason}): {safe_command}"
    evidence = _single_line(redact(result.output.strip(), state.secrets))[: limits.VERIFY_DETAIL_CHARS]
    if evidence:
        detail = f"{detail}; output: {evidence}"
    return VerifyResult(ok=False, stage=STAGE_COMMAND, detail=detail)
