"""Whether the claimed deliverable is real, decided by the environment, not the model.

`docs/research/verifier-contract.md` looked at all six public task verifiers and
found the same shape every time: each reads one absolute path or one HTTP response,
none of them reads agent stdout, and each is binary on that one signal. `finish` in
`agent/prompt.py` asks the model for a `path` and a `verify` command; this module is
what actually decides whether that claim holds, by touching the same two things a real
verifier would -- the file, and the exit code of a command run against it.

The outcomes, in the order they are checked:

* `existence`  -- the path is not a file at all. Every public verifier starts here.
* `non-empty`  -- the file is there but empty; no public verifier accepts that either.
* `exact-file` -- the instruction itself fixed the bytes, so nothing else decides.
* `liveness`   -- something is still running code this run changed. It is restarted
  first and its ports have to answer again; a port that does not come back, or an HTTP
  service that answers 5xx after the change when it did not before, fails here whatever
  the model's command would have said. See the section on it below.
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

import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent import detectors, limits, prompt, result_contract, result_progress, tools
from agent.paths import digest, permits
from agent.redact import redact
from agent.state import AgentState

STAGE_EXISTENCE = "existence"
STAGE_REFUSED = "refused"
STAGE_STALE = "stale"
STAGE_NON_EMPTY = "non-empty"
STAGE_COMMAND = "command"
STAGE_UNVERIFIED = "unverified"
STAGE_EXACT_FILE = "exact-file"
STAGE_LIVENESS = "liveness"


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


# --- The environment the check runs in --------------------------------------
#
# Measured over twelve episodes of two public code-fix tasks (JOURNAL, 16.09.2026):
# every episode ran the project's own test suite as its verify command, and every one
# of those suites talked over HTTP to a service the container had started once, at
# entry, without reload. The suite therefore measured the code as it was *before* the
# run edited it -- four episodes finished `solved=True` on a file whose endpoint
# answered 500, and the one episode that had actually repaired the file was recorded
# as a failure. No exit code could have separated them.
#
# So this module owns the environment its check runs in. A process still running code
# the run has changed is restarted first -- same argv, same cwd, same environment, all
# read back from procfs -- and the ports it was listening on have to answer again
# before the model's command is believed. Every grader in the public set stops and
# restarts the service itself before grading, so restarting it here cannot change a
# score; it only lets the agent see its own work.
#
# The bias throughout: act only on what procfs states plainly, restart the fewest
# processes that can explain the change, and when anything is ambiguous do nothing and
# say so in the detail. A false red costs a solved task, and a reload that kills the
# wrong process costs the whole run.

RELOAD_BUDGET_S = 20.0
"""Ceiling on the whole reload: signals, spawn and waiting for ports to come back."""

RELOAD_BUDGET_FRACTION = 0.5
"""Share of the time left the reload may take. The model's command still has to run."""

RELOAD_MIN_BUDGET_S = 6.0
"""Below this there is no time to stop a service and see it return, so it is left
alone and the detail says the check may be reading pre-change code."""

RELOAD_TERMINATE_GRACE_S = 3.0
"""SIGTERM to SIGKILL. A web server closes its listening socket well inside this."""

RELOAD_PORT_WAIT_S = 15.0
"""How long a restarted service has to listen again before that counts as a failure."""

RELOAD_START_MARGIN_S = 1.0
"""Slack when comparing a process's start time against a file's mtime. A process that
started after the edit already has the new code; the margin covers coarse clocks."""

RELOAD_MAX_SERVICES = 3
"""More independent services than this referencing the same change is not a picture we
can act on safely, so none are touched."""

RELOAD_MAX_MODIFIED = 32
"""Changed files considered when matching processes. A large refactor is still one
service; scanning every path would only cost time."""

PROBE_TIMEOUT_S = 2.0
PROBE_MAX_PORTS = 4
PROBE_SERVER_ERROR = 500
"""A status at or above this is the service declaring itself broken."""

SERVICE_NOTE_CHARS = 240
"""Bound on the reload/liveness text appended to a detail. The detail is one prompt
line, and the model needs the command's own output more than it needs this."""

ENV_NOTE_PREFIX = " [env: "
ENV_NOTE_SUFFIX = "]"
"""Delimiters so the reload note is separable from the command text that precedes it --
`_already_passed` matches on that boundary."""

ANY_ADDRESS = {"0.0.0.0": "127.0.0.1", "::": "::1", "0": "127.0.0.1"}
"""Wildcard bind addresses are not connectable; the loopback of the same family is."""

GENERIC_DIRECTORIES = frozenset(
    {
        "/",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib64",
        "/media",
        "/mnt",
        "/opt",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/srv",
        "/sys",
        "/tmp",
        "/usr",
        "/var",
    }
)
"""A process sitting in one of these tells us nothing about what it loads: every file
on the machine is "under" `/`. Only a working directory more specific than these is
taken as evidence that a change inside it belongs to that process."""


@dataclass(frozen=True)
class ServiceCheck:
    """What the reload found. `failures` turn the whole verification red."""

    notes: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    reloaded: int = 0

    def note(self) -> str:
        """One bounded line for the model, or "" when there is nothing to report.

        Single-lined like every other detail: a stray newline out of an OS error message
        would break the one-event-per-line shape the telemetry renders.
        """
        return _single_line("; ".join((*self.failures, *self.notes)))[:SERVICE_NOTE_CHARS]


def _source_like(path: Path) -> bool:
    """Whether a changed file is the kind of thing a running service loads."""
    return (
        path.name in result_progress.SOURCE_FILENAMES
        or path.suffix.lower() in result_progress.SOURCE_SUFFIXES
    )


def _existed_before(state: AgentState, path: Path) -> bool:
    """Whether this path was already on disk when the run started.

    A file the run *created* is a report, a note or a patch file -- nothing a service
    that started before the run can be loading. A file the run *edited* is the case
    this whole section exists for. Both snapshots are consulted as existence oracles
    only, so an incomplete scan still answers for the paths it did reach.
    """
    snapshot = state.source_snapshot
    if snapshot is not None:
        try:
            relative = path.relative_to(state.workspace.resolve()).as_posix()
        except (OSError, ValueError):
            relative = ""
        if relative and relative in snapshot.files:
            return True
    return bool(state.baseline.get(str(path)))


def _modified_sources(state: AgentState, override: Iterable[Path] | None = None) -> tuple[Path, ...]:
    """Existing production files this run changed, resolved and deduplicated.

    Two sources, because neither alone is complete: `state.artifacts` knows every file
    written through a tool but nothing about a `sed -i` from the shell, and the source
    snapshot sees any edit under the workspace but is bounded and can be incomplete.
    """
    found: dict[Path, None] = {}

    def keep(candidate: Path) -> None:
        try:
            resolved = candidate.resolve()
        except OSError:
            return
        if resolved not in found and resolved.is_file():
            found[resolved] = None

    if override is not None:
        for path in override:
            keep(path)
        return tuple(list(found)[:RELOAD_MAX_MODIFIED])

    snapshot = state.source_snapshot
    if snapshot is not None and snapshot.known:
        try:
            changes = result_progress.changed_sources(state.workspace, snapshot)
        except OSError:
            changes = None
        if changes is not None and changes.known:
            for relative in changes.paths:
                if relative in snapshot.files:  # edited, not created by this run
                    keep(state.workspace / relative)

    for artifact in state.artifacts:
        try:
            resolved = artifact.resolve()
        except OSError:
            continue
        if _source_like(resolved) and _existed_before(state, resolved):
            keep(resolved)

    return tuple(list(found)[:RELOAD_MAX_MODIFIED])


def _token_targets(token: str, cwd: Path | None) -> list[Path]:
    """Paths an argv token could be naming, resolved the way the process would see them.

    `main:app`, `--app-dir=/srv/api`, `app.main:app` and `./server.js` are all ordinary
    ways to name the code a server loads, so each token is reduced to its path-shaped
    part and offered in both file and dotted-module form.
    """
    if not token or (token.startswith("-") and "=" not in token):
        return []
    head = token.split("=", 1)[1] if token.startswith("-") else token
    head = head.split(":", 1)[0].strip().strip("'\"")
    if not head or head in {".", ".."}:
        return []

    shapes = [head]
    if "/" not in head and not head.startswith("."):
        # `main:app`, `app.main:app` and `-m app` all name a module, not a file. Both
        # module shapes are offered: a module is a file, a package is a directory with
        # `__init__.py`. Without this, the most common service line in the public set --
        # `uvicorn main:app` from the application root -- matches nothing at all.
        dotted = head.replace(".", "/")
        shapes.append(dotted + ".py")
        shapes.append(dotted + "/__init__.py")

    targets: list[Path] = []
    for shape in shapes:
        candidate = Path(shape)
        if not candidate.is_absolute():
            if cwd is None:
                continue
            candidate = cwd / shape
        try:
            # Symlinks and `..` are resolved on both sides of every comparison: procfs
            # hands back a fully resolved cwd, and the changed paths are resolved too,
            # so an argv written against a symlinked project root still matches.
            targets.append(candidate.resolve())
        except OSError:
            targets.append(candidate)
    return targets


def _is_under(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return path != directory


def _is_entry_point(target: Path) -> bool:
    """Whether an existing path a command line names could be the code it runs.

    Source files and extension-less programs qualify; a log or a data file the process
    merely holds open does not. Without this a `tail -f /srv/app/access.log` would look
    exactly like the server it is following.
    """
    try:
        if target.is_dir():
            return True
        if not target.is_file():
            return False
    except OSError:
        return False
    return _source_like(target) or not target.suffix


def _references(process: tools.ProcessInfo, modified: Sequence[Path]) -> str:
    """Why this process would be running changed code, or "" if there is no reason to
    think it is.

    Two rules, both drawn from how the services in the public set are actually started.

    * The command line names a changed file, or a directory holding one: `python
      /srv/api/server.py`, `gunicorn --chdir /srv/api ...`.
    * The command line names an entry point -- a script, a module, a package -- and a
      changed file lives in the directory that entry point was loaded from. That is
      what `uvicorn main:app` from the application root looks like: nothing in the
      command line mentions the router that changed, but `main.py` sits beside it and
      imports it.

    The entry point is what keeps the second rule honest. A shell, an editor or a `tail`
    sitting in the same directory names no code, so none of them qualify.
    """
    cwd = process.cwd
    entries: list[Path] = []
    for token in process.argv:
        for target in _token_targets(token, cwd):
            for path in modified:
                if target == path:
                    return f"argv names {path.name}"
                if _is_under(path, target) and str(target) not in GENERIC_DIRECTORIES:
                    return f"argv names {target.name}/, holding {path.name}"
            if _is_entry_point(target):
                entries.append(target)

    for entry in entries:
        try:
            root = entry.parent if entry.is_file() else entry
        except OSError:
            continue
        if str(root) in GENERIC_DIRECTORIES:
            continue
        inside = [path for path in modified if _is_under(path, root)]
        if inside:
            return f"loads {entry.name} from {root}, where {inside[0].name} changed"
    return ""


def _service_name(process: tools.ProcessInfo, secrets: Sequence[str]) -> str:
    """A short, safe name for the log: the program and the thing it was pointed at.

    Only these two tokens. A full command line can carry a database password or a token
    as an option value, and this text goes to the model and into the trace.
    """
    name = Path(process.argv[0]).name if process.argv else f"pid {process.pid}"
    for token in process.argv[1:]:
        if not token.startswith("-"):
            name = f"{name} {token}"
            break
    return _single_line(redact(name, secrets))[:80]


def _probe_address(address: str) -> str:
    return ANY_ADDRESS.get(address, address)


def _ports_of(pid: int, table: Sequence[tools.ProcessInfo], sockets: Sequence[tools.ListeningSocket]) -> set[tuple[str, int]]:
    """Listening ports held by this process or one of its workers, as far as procfs says.

    Empty means "we could not tell", never "it has none": `/proc/<pid>/fd` is readable
    only for our own user, and a worker can hold the inherited socket while the master
    does not.
    """
    inodes: set[int] = set(tools.socket_inodes(pid))
    for child in tools.process_descendants(pid, table):
        inodes |= set(tools.socket_inodes(child))
    return {(entry.address, entry.port) for entry in sockets if entry.inode in inodes}


def _listening_pairs() -> set[tuple[str, int]]:
    return {(entry.address, entry.port) for entry in tools.listening_sockets()}


def _wait_for_ports(
    ports: Iterable[tuple[str, int]], deadline: float
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Wait until each port accepts a connection again. Returns (back, still missing)."""
    pending = list(ports)
    back: list[tuple[str, int]] = []
    while pending and time.monotonic() < deadline:
        for port in list(pending):
            if tools.port_open(_probe_address(port[0]), port[1], timeout_s=0.25):
                pending.remove(port)
                back.append(port)
        if pending:
            time.sleep(tools.PROC_POLL_S)
    return back, pending


def _protected_pids(table: Sequence[tools.ProcessInfo]) -> frozenset[int]:
    """Processes this check must never signal: init, ourselves, our parents, our children.

    PID 1 is the container's entrypoint -- in the public images a shell holding the
    container open with `tail -f /dev/null`, whose death ends the task. Our own tree is
    the agent, the runner above it and anything a tool of ours has started.
    """
    mine = os.getpid()
    protected = {0, 1, mine}
    protected |= tools.process_ancestors(mine, table)
    protected |= tools.process_descendants(mine, table)
    return frozenset(protected)


def _reload_service(
    process: tools.ProcessInfo,
    *,
    ports: set[tuple[str, int]],
    deadline: float,
    secrets: Sequence[str],
) -> tuple[list[str], list[str], bool]:
    """Restart one service in place. Returns (notes, failures, restarted)."""
    name = _service_name(process, secrets)
    notes: list[str] = []

    if process.cwd is None:
        return [f"{name} not reloaded: its working directory is unreadable"], [], False
    environ = tools.process_environ(process.pid)
    if environ is None:
        # Restarting into a different environment is how a service loses its database
        # URL and fails for a reason the run did not cause.
        return [f"{name} not reloaded: its environment is unreadable"], [], False
    if not tools.executable_exists(process.argv[0], environ):
        return [f"{name} not reloaded: {Path(process.argv[0]).name} is no longer executable"], [], False

    before = _listening_pairs()
    baseline = {
        port: tools.http_probe(_probe_address(port[0]), port[1], timeout_s=PROBE_TIMEOUT_S)
        for port in sorted(ports)[:PROBE_MAX_PORTS]
    }

    refusal = tools.terminate_tree(
        process.pid,
        # Read again here rather than reusing the scan: a master that forked a worker
        # since then would otherwise keep that worker, and the worker keeps the
        # listening socket the restarted master needs.
        tools.process_table(),
        grace_s=min(RELOAD_TERMINATE_GRACE_S, max(0.5, deadline - time.monotonic())),
    )
    if refusal:
        # It is still serving the old code, which is exactly what makes the check blind;
        # saying so is all we can honestly do about it.
        return [f"{name} could not be stopped ({refusal}); the check may read pre-change code"], [], False

    after = _listening_pairs()
    expected = ports or (before - after)
    stubborn = sorted(port for _, port in ports & after)
    if stubborn:
        # Another process -- a worker that outlived its master, a second instance -- is
        # still holding it, so the restart below cannot bind it and what answers may
        # still be the old code.
        notes.append(f"port {stubborn[0]} is still held by another process after the reload")
    pid, error = tools.start_detached(process.argv, cwd=process.cwd, env=environ)
    if pid is None:
        return [], [f"{name} was stopped for the reload and did not start again: {error}"], True

    notes.append(f"reloaded {name} (pid {process.pid} -> {pid})")
    if not expected:
        return notes, [], True

    back, missing = _wait_for_ports(expected, min(deadline, time.monotonic() + RELOAD_PORT_WAIT_S))
    failures: list[str] = []
    for _, port in missing:
        failures.append(f"port {port} did not listen again within {RELOAD_PORT_WAIT_S:.0f}s after reloading {name}")
    for address, port in sorted(back)[:PROBE_MAX_PORTS]:
        failures.extend(_probe_failures(address, port, baseline.get((address, port)), name, notes))
    return notes, failures, True


def _probe_failures(
    address: str,
    port: int,
    baseline: tools.HttpProbe | None,
    name: str,
    notes: list[str],
) -> list[str]:
    """Judge one live port. Appends what it saw to `notes`, returns what makes it red."""
    probe = tools.http_probe(_probe_address(address), port, timeout_s=PROBE_TIMEOUT_S)
    if not probe.connected:
        return [f"port {port} refused a connection after the change ({name})"]
    if not probe.spoke_http or probe.status is None:
        notes.append(f"port {port} is not answering HTTP; liveness not judged")
        return []
    if probe.status < PROBE_SERVER_ERROR:
        notes.append(f"port {port} answered HTTP {probe.status} on GET /")
        return []
    if baseline is not None and baseline.status is not None and baseline.status >= PROBE_SERVER_ERROR:
        # It answered the same way before the reload, so the change did not cause it and
        # failing the run for it would be inventing a regression.
        notes.append(f"port {port} answered HTTP {probe.status} on GET /, as it did before the reload")
        return []
    return [f"port {port} answered HTTP {probe.status} on GET / after the change ({name})"]


def _service_check(state: AgentState, modified: Sequence[Path]) -> ServiceCheck:
    """Reload whatever is running the changed code, then ask the ports how they are.

    Everything is best effort except the verdict: a port that does not come back, or an
    HTTP service that answers 5xx after the change when it did not before, is a failure
    the loop can recover from. An unverified deliverable is not a verified one.
    """
    if not modified or os.name != "posix":
        return ServiceCheck()

    allowance = min(RELOAD_BUDGET_S, max(0.0, state.budget.remaining_s()) * RELOAD_BUDGET_FRACTION)
    if allowance < RELOAD_MIN_BUDGET_S:
        return ServiceCheck(
            notes=("too little time left to reload a service; the check may read pre-change code",)
        )
    deadline = time.monotonic() + allowance

    table = tools.process_table()
    if not table:
        return ServiceCheck(notes=("the process table is unreadable; nothing was reloaded",))

    protected = _protected_pids(table)
    candidates = [
        process
        for process in table
        if process.pid not in protected and _references(process, modified)
    ]
    if not candidates:
        return ServiceCheck()

    known = {process.pid for process in candidates}
    roots = [process for process in candidates if process.ppid not in known]
    if len(roots) > RELOAD_MAX_SERVICES:
        return ServiceCheck(
            notes=(f"{len(roots)} processes reference the changed files; none reloaded, too ambiguous",)
        )

    newest = 0.0
    for path in modified:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue

    sockets = tools.listening_sockets()
    notes: list[str] = []
    failures: list[str] = []
    reloaded = 0
    for process in roots:
        ports = _ports_of(process.pid, table, sockets)
        if process.started_at and newest and process.started_at > newest + RELOAD_START_MARGIN_S:
            # Started after the last edit, so it already runs the new code. Still worth
            # a probe: this is the shape a service the run restarted itself arrives in.
            notes.append(f"{_service_name(process, state.secrets)} already started after the change")
            for address, port in sorted(ports)[:PROBE_MAX_PORTS]:
                failures.extend(
                    _probe_failures(address, port, None, _service_name(process, state.secrets), notes)
                )
            continue
        if time.monotonic() + RELOAD_MIN_BUDGET_S > deadline:
            notes.append(f"{_service_name(process, state.secrets)} not reloaded: out of reload time")
            continue
        service_notes, service_failures, restarted = _reload_service(
            process,
            ports=ports,
            deadline=deadline,
            secrets=state.secrets,
        )
        notes.extend(service_notes)
        failures.extend(service_failures)
        reloaded += int(restarted)

    return ServiceCheck(notes=tuple(notes), failures=tuple(failures), reloaded=reloaded)


def _success_detail(command: str) -> str:
    """The exact text a passing command produces, so a later call can recognise it."""
    return f"verify command exited 0: {command}"


def _with_note(detail: str, note: str) -> str:
    """Append what the reload did, behind a delimiter, or leave the detail untouched."""
    return f"{detail}{ENV_NOTE_PREFIX}{note}{ENV_NOTE_SUFFIX}" if note else detail


def _already_passed(state: AgentState, command: str, safe_command: str) -> bool:
    """Whether this identical command already succeeded earlier in this same run.

    Two places, because the measured case is in the second one. A previous verification
    is in the trace the loop writes; the model running the same suite itself first is in
    the action history, where arguments are stored as a digest -- so the digest is
    recomputed here from the argument shape the loop uses for a shell call and compared,
    which keeps the command text out of the comparison entirely.

    `verify(..., already_passed=...)` overrides both, for a caller that keeps its own
    history. The reload note is appended behind a delimiter precisely so a longer
    command cannot be mistaken for a prefix match on a shorter one.
    """
    marker = _success_detail(safe_command)
    for event in state.audit:
        if event.kind != "verify":
            continue
        if event.detail == marker or event.detail.startswith(marker + ENV_NOTE_PREFIX):
            return True
    shapes = {detectors.args_digest({"command": text}) for text in (command, command.strip())}
    return any(
        record.tool == prompt.RUN_SHELL and record.ok and record.args_digest in shapes
        for record in state.actions
    )


NON_IDEMPOTENT_HINT = (
    "this exact command already passed earlier in this run, so the suite may not be "
    "idempotent (a test that inserts a fixed row fails on its second run); read the "
    "failing assertion before editing the file again"
)


def verify(
    state: AgentState,
    *,
    deliverable: Path,
    command: str,
    modified: Iterable[Path] | None = None,
    already_passed: bool | None = None,
) -> VerifyResult:
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

    Before the command runs, anything still executing code this run has changed is
    reloaded and its ports are asked whether they are alive -- see the section above.
    `modified` overrides which files count as changed (the default reads them from the
    run's own artifacts and source snapshot); `already_passed` overrides whether this
    exact command has already succeeded in this run (the default reads the loop's
    trace). Both exist so a caller keeping its own history can supply it, and both
    default to "work it out from `state`", which is what the loop relies on.
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

    try:
        service = _service_check(state, _modified_sources(state, modified))
    except Exception as error:  # noqa: BLE001 - see below
        # Deliberately broad. This section walks procfs, signals processes and opens
        # sockets while the machine is changing under it; every such call is guarded,
        # but a verification that crashes is a task scored zero, whereas a verification
        # that says it could not look is a task that can still be finished.
        service = ServiceCheck(notes=(f"service reload skipped after {type(error).__name__}",))
    if service.reloaded:
        # A restarted service invalidates every observation taken from the old process.
        state.advance_state_epoch()
    note = service.note()
    if service.failures:
        # Red before the command even runs: whatever it would have printed, it would
        # have been talking to a service that is not serving the changed code.
        return VerifyResult(ok=False, stage=STAGE_LIVENESS, detail=note)

    stripped = command.strip()
    if not stripped:
        return VerifyResult(
            ok=True,
            stage=STAGE_UNVERIFIED,
            detail=_with_note(
                f"{deliverable} exists and is non-empty; no verify command was given", note
            ),
        )

    result = tools.run_shell(
        stripped,
        cwd=state.workspace,
        timeout_s=state.budget.tool_timeout_s(),
        secrets=state.secrets,
    )
    safe_command = _single_line(redact(stripped, state.secrets))
    if result.ok:
        return VerifyResult(
            ok=True, stage=STAGE_COMMAND, detail=_with_note(_success_detail(safe_command), note)
        )

    reason = _single_line(result.detail or f"exit {result.exit_code}")
    detail = f"verify command failed ({reason}): {safe_command}"
    evidence = _single_line(redact(result.output.strip(), state.secrets))[: limits.VERIFY_DETAIL_CHARS]
    if evidence:
        detail = f"{detail}; output: {evidence}"
    repeated = (
        _already_passed(state, command, safe_command) if already_passed is None else already_passed
    )
    if repeated:
        # Measured: one project's suite inserted a fixed row, so its second identical
        # run failed on a unique constraint and burned the single recovery pass on an
        # already-correct file. Never treated as success -- only named, so the model
        # reads the assertion before it edits anything.
        detail = f"{detail}; {NON_IDEMPOTENT_HINT}"
    return VerifyResult(ok=False, stage=STAGE_COMMAND, detail=_with_note(detail, note))
