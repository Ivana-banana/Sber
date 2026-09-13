"""What the agent is allowed to do to the machine, and what may come back.

Every tool here returns the same small record instead of raising, for the reason
recorded in `agent/ports.py`: a failure is a value the loop reasons about, and an
exception is a task scored zero. A tool that cannot run says so; the loop decides
whether that means retry, replan or finish.

Two bounds are enforced here rather than left to the model:

* **time** -- a command that hangs is killed, together with anything it spawned, and
  the step is handed back to the loop;
* **size** -- an observation is cut to a head and a tail before it can reach the
  context window. EnIGMA reports sessions ending outright because one `strings` or
  decompiler dump filled the window, and multi-megabyte evidence files are the norm
  in the forensic tasks we expect. The cut is always visible in the text.

Standard library only, same as the rest of `agent/`.
"""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent import limits
from agent.paths import permits
from agent.redact import AGENT_CREDENTIAL_ENV_NAMES, redact

CHUNK_BYTES = 65536
KILL_GRACE_S = 5.0
# How long past the command's own timeout to let the reader threads finish draining
# before concluding a backgrounded child is holding the pipe and reaping the session.
# Short on purpose: this is slack over the budget, not a second timeout.
READER_GRACE_S = 0.5
SKIP_DIRECTORIES = frozenset({".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"})
SYSTEM_DIRECTORIES = frozenset({"/proc", "/sys", "/dev"})
"""Kernel pseudo-filesystems never walked. A `search("flag{", "/")` would otherwise
descend into `/proc/<pid>/fd`, whose entries block or stream without end. Matched by
absolute path, so a task's own subdirectory merely named `dev` is not skipped."""
MATCH_LINE_CHARS = 200


@dataclass(frozen=True)
class ToolResult:
    """One observation, already bounded and already redacted."""

    name: str
    ok: bool
    output: str = ""
    detail: str = ""
    """Why it failed, short enough to put in a prompt. Empty when it did not."""

    exit_code: int | None = None
    duration_s: float = 0.0
    truncated: bool = False


def clip(
    text: str,
    *,
    head: int = limits.TOOL_OUTPUT_HEAD_BYTES,
    tail: int = limits.TOOL_OUTPUT_TAIL_BYTES,
) -> tuple[str, bool]:
    """Cut the middle out of `text`, keeping both ends and saying how much went."""
    if len(text) <= head + tail:
        return text, False
    dropped = len(text) - head - tail
    marker = f"\n… [truncated {dropped} chars] …\n"
    if dropped <= len(marker):
        # Cutting here would add more characters than it removed. An observation that
        # grows when bounded is worse than one left alone.
        return text, False
    return f"{text[:head]}{marker}{text[-tail:]}", True


class _BoundedSink:
    """A pipe reader that keeps the first and last bytes and forgets the rest.

    The pipe is drained to the end regardless, because a child blocked on a full pipe
    never exits and would turn a size problem into a hang.
    """

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def drain(self, stream: object) -> None:
        read = getattr(stream, "read", None)
        if read is None:
            return
        while True:
            chunk = read(CHUNK_BYTES)
            if not chunk:
                break
            self.feed(chunk)

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = limits.TOOL_OUTPUT_HEAD_BYTES - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if not chunk:
            return
        self.tail += chunk
        if len(self.tail) > 2 * limits.TOOL_OUTPUT_TAIL_BYTES:
            del self.tail[: -limits.TOOL_OUTPUT_TAIL_BYTES]

    def text(self) -> tuple[str, bool]:
        kept = len(self.head) + min(len(self.tail), limits.TOOL_OUTPUT_TAIL_BYTES)
        if self.total <= kept:
            return (bytes(self.head) + bytes(self.tail)).decode("utf-8", "replace"), False
        head = bytes(self.head).decode("utf-8", "replace")
        tail = bytes(self.tail[-limits.TOOL_OUTPUT_TAIL_BYTES :]).decode("utf-8", "replace")
        return f"{head}\n… [truncated {self.total - kept} bytes] …\n{tail}", True


def _kill_tree(process: subprocess.Popen[bytes], pgid: int | None = None) -> None:
    """Kill the child and everything it started.

    `sh -c "cmd &"` returns immediately and leaves the background job running; killing
    only the shell leaves that job holding the CPU for the rest of the run. On POSIX
    the child gets its own session, so one `killpg` reaches all of it.

    `pgid` is captured at spawn time and passed in because the group must be reachable
    even after the shell itself has been reaped: once `process.wait()` collects the
    leader, `os.getpgid(process.pid)` raises `ESRCH`, but the group id (equal to the
    leader's pid under `start_new_session`) still names the surviving background job.
    """
    if os.name == "posix":
        target = pgid
        if target is None:
            try:
                target = os.getpgid(process.pid)
            except OSError:
                target = None
        if target is not None:
            try:
                os.killpg(target, signal.SIGKILL)
                return
            except OSError:
                pass
    process.kill()


def run_argv(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    secrets: Iterable[str] = (),
    name: str = "run",
    env: Mapping[str, str] | None = None,
) -> ToolResult:
    """Run a command with no shell between us and it, bounded in time and size.

    `env` replaces the child's whole environment rather than extending it, which is
    what makes it usable as a scrubber: `run_shell` and the macro controller both hand
    the child an environment with the endpoint credentials removed
    (`endpoint_scrubbed_env`), and an "extend" semantics would have left them in place
    while looking like it had not. `None` keeps the parent environment; callers that run
    the model's own commands pass a scrubbed env instead, so a direct `run_argv` with a
    real command and `env=None` would inherit the key -- there is no such caller today.
    """
    started = time.monotonic()
    spawn: dict[str, object] = {}
    if os.name == "posix":
        spawn["start_new_session"] = True
    if env is not None:
        spawn["env"] = dict(env)

    # argv is a list, so the model's text is one argument and never a command line.
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **spawn,  # type: ignore[arg-type]
        )
    except (OSError, ValueError) as exc:
        return ToolResult(
            name=name,
            ok=False,
            detail=f"could not start: {exc.__class__.__name__}: {exc}",
            duration_s=time.monotonic() - started,
        )

    # Captured now, while the leader is alive: after `process.wait()` reaps it,
    # `os.getpgid` can no longer find the group that a backgrounded job still holds.
    pgid: int | None = None
    if os.name == "posix":
        try:
            pgid = os.getpgid(process.pid)
        except OSError:
            pgid = None

    out_sink, err_sink = _BoundedSink(), _BoundedSink()
    readers = [
        threading.Thread(target=out_sink.drain, args=(process.stdout,), daemon=True),
        threading.Thread(target=err_sink.drain, args=(process.stderr,), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=max(0.0, timeout_s))
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(process, pgid)
        try:
            # Bounded on purpose: a process that survives SIGKILL is stuck in the
            # kernel, and waiting on it forever is the hang we came here to prevent.
            process.wait(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            pass

    # The shell can exit while a job it backgrounded (`cmd &`) still holds the write
    # end of our pipes. The reader threads -- and the `close()` below, which contends
    # for their buffer lock -- then block on that job until it ends, which is the
    # freeze this timeout exists to prevent. Wait for the readers only within the
    # command's own budget; if they are still blocked, a lingering child is why, so
    # reap the whole session (start_new_session put the job in it) to release the pipe.
    reader_deadline = started + max(0.0, timeout_s) + READER_GRACE_S
    for reader in readers:
        reader.join(timeout=max(0.0, reader_deadline - time.monotonic()))
    if any(reader.is_alive() for reader in readers):
        timed_out = True
        _kill_tree(process, pgid)
        for reader in readers:
            reader.join(timeout=KILL_GRACE_S)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    stdout, out_cut = out_sink.text()
    stderr, err_cut = err_sink.text()
    body = stdout
    if stderr.strip():
        body = f"{body}\n[stderr]\n{stderr}" if body.strip() else f"[stderr]\n{stderr}"
    body, body_cut = clip(body)

    exit_code = process.returncode
    return ToolResult(
        name=name,
        ok=not timed_out and exit_code == 0,
        output=redact(body, secrets),
        detail=f"timeout after {timeout_s:.0f}s" if timed_out else "",
        exit_code=None if timed_out else exit_code,
        duration_s=time.monotonic() - started,
        truncated=out_cut or err_cut or body_cut,
    )


def endpoint_scrubbed_env() -> dict[str, str]:
    """The process environment with the agent's endpoint credentials removed.

    A shell the model runs can `env`, `env > file`, `echo $OPENAI_API_KEY | base64` or
    `cat /proc/self/environ`. Removing the three variables before the child starts means
    the child's own environment (and `/proc/self/environ`) never carries them, so none
    of these reach the key -- including the encoded forms that substring redaction
    cannot catch. It does not by itself scrub the *parent* process's environment; `main`
    pops the same variables from `os.environ` so `/proc/$PPID/environ` is closed too.
    Everything else -- PATH and the task's own environment the command legitimately
    needs -- is kept.
    """
    hidden = set(AGENT_CREDENTIAL_ENV_NAMES)
    return {name: value for name, value in os.environ.items() if name not in hidden}


def run_shell(
    command: str,
    *,
    cwd: Path,
    timeout_s: float,
    secrets: Iterable[str] = (),
) -> ToolResult:
    """Run a shell command line, so pipelines and redirection stay available."""
    return run_argv(
        [limits.SHELL_EXECUTABLE, "-c", command],
        cwd=cwd,
        timeout_s=timeout_s,
        secrets=secrets,
        name="shell",
        env=endpoint_scrubbed_env(),
    )


def write_file(
    path: Path,
    content: str,
    *,
    workspace: Path,
    permitted: tuple[Path, ...] = (),
    secrets: Iterable[str] = (),
) -> ToolResult:
    """Write exactly `content` to `path`, creating parents, replacing what was there.

    Exactly, and that word is the whole point. Four of the six public verifiers
    compare a file against a fixed expectation -- shell string equality, a sorted
    `diff`, a JSON parse -- so a courtesy trailing newline or a diagnostic line is not
    a cosmetic difference, it is a zero. Nothing is appended and nothing is formatted.

    Writing goes through a tool rather than through `run_shell` because the shell path
    requires the model to quote the content correctly, and file content in these tasks
    contains the characters that break quoting: newlines, quotes, dollars, backslashes.
    A quoting bug there produces a plausible-looking file with the wrong bytes.

    The write is confined to the workspace and to the paths the task text names.
    `resolve_workspace` is a guess about where the task lives and E011 showed it can be
    wrong, so confining to it alone refuses the one path the task actually asked for
    and leaves the answer in the wrong place. What stays refused is everything neither
    the workspace nor the task named: a file's contents, a log line or a command's
    output cannot move a write, because none of them is the instruction.
    """
    try:
        root = workspace.resolve()
        target = Path(path)
        if not target.is_absolute():
            target = root / target
        target = target.resolve()
    except OSError as exc:
        return ToolResult(name="write_file", ok=False, detail=f"bad path: {exc}")

    if not permits(target, root, permitted):
        return ToolResult(
            name="write_file",
            ok=False,
            detail=f"refused: {target} is outside the workspace {root} and unnamed by the task",
        )

    started = time.monotonic()
    # Encode before opening. A small model that emits a lone surrogate (`\ud83d` from a
    # split emoji escape) made `handle.write` raise UnicodeEncodeError -- but only after
    # open("w") had already truncated the file to zero bytes, and only OSError was
    # caught, so the deliverable was destroyed and replaced with nothing. Encoding first
    # means an unencodable string is refused with the file untouched. Bytes are written
    # in binary mode, which keeps "exactly these bytes" without newline translation.
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        return ToolResult(
            name="write_file",
            ok=False,
            detail=redact(f"content is not encodable as UTF-8: {exc}", secrets),
            duration_s=time.monotonic() - started,
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            handle.write(data)
    except OSError as exc:
        return ToolResult(
            name="write_file",
            ok=False,
            detail=redact(f"{exc.__class__.__name__}: {exc}", secrets),
            duration_s=time.monotonic() - started,
        )

    written = f"wrote {len(content.encode('utf-8'))} bytes to {target}"
    # A byte count is not evidence that a machine-readable deliverable is readable.
    # In E-BASE-r1 the model wrote a finding that satisfied every assertion the
    # verifier makes, lost it to one unescaped quote inside `evidence`, was told
    # "wrote 481 bytes", and spent the rest of its budget replacing its own analysis
    # with `{"findings": []}`. The file is still written -- the deadline checkpoint
    # needs a deliverable on disk -- and nothing here judges what is in it.
    if target.suffix.lower() == ".json":
        try:
            json.loads(content)
        except ValueError as exc:
            written += f"; the file is not valid JSON: {exc}"

    return ToolResult(
        name="write_file",
        ok=True,
        output=written,
        duration_s=time.monotonic() - started,
    )


def replace_text(
    path: Path,
    old: str,
    new: str,
    *,
    workspace: Path,
    permitted: tuple[Path, ...] = (),
    secrets: Iterable[str] = (),
) -> ToolResult:
    """Replace the single occurrence of `old` with `new`, leaving the file otherwise byte-identical.

    The tool that was missing. `write_file` replaces a whole file, and `read_range`
    hands back a window; a model that reads a window and writes its fix back through
    `write_file` deletes everything it did not read. In E-BASE that is exactly what
    happened to a source module whose vulnerable function sits at the bottom: the fix
    itself was correct, and the module lost its imports and stopped importing.

    An anchor and not a line range, because `read_range` prints no line numbers, so
    the model has no numbers to give. It does have the text -- it quoted the block it
    wanted to change verbatim, fourteen times, in the run that motivated this. The
    source must be valid UTF-8 and fit the bounded edit size; otherwise the operation
    refuses before touching the file. The replacement is prepared in a sibling
    temporary file and installed atomically with the original mode.

    Ambiguity is refused rather than resolved. Replacing the first of several matches
    is a coin flip the model cannot see the result of, and the count of matches is
    something it can act on by lengthening the anchor.
    """
    try:
        root = workspace.resolve()
        target = Path(path)
        if not target.is_absolute():
            target = root / target
        target = target.resolve()
    except OSError as exc:
        return ToolResult(name="replace_text", ok=False, detail=f"bad path: {exc}")

    if not permits(target, root, permitted):
        return ToolResult(
            name="replace_text",
            ok=False,
            detail=f"refused: {target} is outside the workspace {root} and unnamed by the task",
        )

    if not old:
        return ToolResult(name="replace_text", ok=False, detail="refused: `old` is empty")
    if old == new:
        return ToolResult(name="replace_text", ok=False, detail="refused: `old` and `new` are the same")

    started = time.monotonic()
    try:
        with target.open("rb") as handle:
            before_bytes = handle.read(limits.REPLACE_MAX_BYTES + 1)
        if len(before_bytes) > limits.REPLACE_MAX_BYTES:
            return ToolResult(
                name="replace_text",
                ok=False,
                detail=(
                    f"refused: {target} is larger than the {limits.REPLACE_MAX_BYTES} "
                    "byte replace limit; no bytes were changed"
                ),
                duration_s=time.monotonic() - started,
            )
        before = before_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return ToolResult(
            name="replace_text",
            ok=False,
            detail=f"refused: {target} is not valid UTF-8; no bytes were changed",
            duration_s=time.monotonic() - started,
        )
    except OSError as exc:
        return ToolResult(
            name="replace_text",
            ok=False,
            detail=redact(f"{exc.__class__.__name__}: {exc}", secrets),
            duration_s=time.monotonic() - started,
        )

    found = before.count(old)
    if found == 0:
        return ToolResult(
            name="replace_text",
            ok=False,
            detail=(
                f"`old` does not appear in {target}. Read the file and copy the text "
                f"to replace exactly, including indentation."
            ),
            duration_s=time.monotonic() - started,
        )
    if found > 1:
        return ToolResult(
            name="replace_text",
            ok=False,
            detail=(
                f"`old` appears {found} times in {target}; the edit would be ambiguous. "
                f"Extend it with surrounding lines until it is unique."
            ),
            duration_s=time.monotonic() - started,
        )

    try:
        replacement = before.replace(old, new, 1).encode("utf-8")
        mode = stat.S_IMODE(target.stat().st_mode)
    except UnicodeEncodeError:
        return ToolResult(
            name="replace_text",
            ok=False,
            detail="refused: replacement is not valid UTF-8; no bytes were changed",
            duration_s=time.monotonic() - started,
        )
    except OSError as exc:
        return ToolResult(
            name="replace_text",
            ok=False,
            detail=redact(f"{exc.__class__.__name__}: {exc}", secrets),
            duration_s=time.monotonic() - started,
        )

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        return ToolResult(
            name="replace_text",
            ok=False,
            detail=redact(f"{exc.__class__.__name__}: {exc}", secrets),
            duration_s=time.monotonic() - started,
        )
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass

    line = before[: before.index(old)].count("\n") + 1
    return ToolResult(
        name="replace_text",
        ok=True,
        output=f"replaced 1 occurrence at line {line} of {target}",
        duration_s=time.monotonic() - started,
    )


def _is_regular_file(path: Path) -> bool:
    """True only for a real regular file.

    A FIFO blocks `open()` until a writer appears, a character device or a `/proc` entry
    can stream without end, and a socket cannot be opened for reading -- any of which
    turns a read tool into a hang the runner has to kill. `stat` follows symlinks but
    reads metadata without opening, so this is safe to call on the special file itself.
    """
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except OSError:
        return False


def _looks_binary(path: Path) -> bool:
    with path.open("rb") as handle:
        return b"\x00" in handle.read(limits.BINARY_SNIFF_BYTES)


def file_info(path: Path) -> ToolResult:
    """Size, kind and line count, so the model can choose how to read a file.

    Deliberately the cheapest tool in the set. Asking it first is what stops the loop
    from `cat`-ing a 200 MB evidence file to find out how big it is.
    """
    try:
        stat = path.stat()
    except OSError as exc:
        return ToolResult(name="file_info", ok=False, detail=f"{exc.__class__.__name__}: {exc}")

    if path.is_dir():
        return ToolResult(name="file_info", ok=True, output=f"{path.name}: directory")

    if not path.is_file():
        # A FIFO, socket or device: real, but not something to open. Say so rather than
        # block on it or call it binary.
        return ToolResult(name="file_info", ok=True, output=f"{path.name}: not a regular file")

    try:
        if _looks_binary(path):
            return ToolResult(name="file_info", ok=True, output=f"{path.name}: {stat.st_size} bytes, binary")
        lines = 0
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK_BYTES):
                lines += chunk.count(b"\n")
    except OSError as exc:
        return ToolResult(name="file_info", ok=False, detail=f"{exc.__class__.__name__}: {exc}")

    return ToolResult(name="file_info", ok=True, output=f"{path.name}: {stat.st_size} bytes, text, {lines} lines")


def read_range(
    path: Path,
    *,
    start_line: int = 1,
    max_lines: int = limits.READ_MAX_LINES,
    secrets: Iterable[str] = (),
) -> ToolResult:
    """Read a window of a text file by line number.

    A window and not a file: the forensic tasks we expect ship logs far larger than
    any context window, and the only safe read is one whose size is decided before the
    file is opened rather than after.
    """
    start_line = max(1, start_line)
    max_lines = max(1, min(max_lines, limits.READ_MAX_LINES))

    if not _is_regular_file(path):
        return ToolResult(name="read_range", ok=False, detail=f"not a regular file: {path}")

    collected: list[str] = []
    used = 0
    truncated = False
    last_line = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, start=1):
                last_line = number
                if number < start_line:
                    continue
                if len(collected) >= max_lines:
                    truncated = True
                    break
                room = limits.READ_MAX_BYTES - used
                if room <= 0:
                    truncated = True
                    break
                if len(line) > room:
                    collected.append(line[:room])
                    truncated = True
                    break
                collected.append(line)
                used += len(line)
    except OSError as exc:
        return ToolResult(
            name="read_range",
            ok=False,
            detail=redact(f"{exc.__class__.__name__}: {exc} ({path})", secrets),
        )

    if not collected:
        return ToolResult(
            name="read_range",
            ok=False,
            detail=redact(f"no line {start_line}: file has {last_line} lines ({path})", secrets),
        )

    body = "".join(collected)
    if truncated:
        body += f"\n… [window cut at line {start_line + len(collected) - 1}] …"
    end_line = start_line + len(collected) - 1
    metadata = (
        f"[read_range source={redact(str(path), secrets)} start_line={start_line} "
        f"end_line={end_line} eof={'no' if truncated else 'yes'} "
        f"truncated={'yes' if truncated else 'no'}]"
    )
    return ToolResult(
        name="read_range",
        ok=True,
        output=f"{redact(body, secrets)}\n{metadata}",
        truncated=truncated,
    )


def list_dir(
    path: Path,
    *,
    limit: int = limits.LIST_MAX_ENTRIES,
    secrets: Iterable[str] = (),
) -> ToolResult:
    """List one directory, bounded, directories marked with a trailing slash."""
    try:
        entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name))
    except OSError as exc:
        return ToolResult(
            name="list_dir",
            ok=False,
            detail=redact(f"{exc.__class__.__name__}: {exc} ({path})", secrets),
        )

    shown, lines = entries[:limit], []
    for entry in shown:
        if entry.is_dir():
            lines.append(f"{entry.name}/")
            continue
        try:
            lines.append(f"{entry.name} ({entry.stat().st_size} B)")
        except OSError:
            lines.append(f"{entry.name} (unreadable)")

    truncated = len(entries) > len(shown)
    if truncated:
        lines.append(f"… {len(entries) - len(shown)} more entries")
    metadata = (
        f"[list_dir source={redact(str(path), secrets)} entries_shown={len(shown)} "
        f"entries_total={len(entries)} truncated={'yes' if truncated else 'no'}]"
    )
    body = "\n".join(lines)
    return ToolResult(
        name="list_dir",
        ok=True,
        output=f"{body}\n{metadata}" if body else metadata,
        truncated=truncated,
    )


def _files_under(root: Path) -> Iterable[Path]:
    """Every regular text-candidate file `search` will open, in a stable order.

    Kernel pseudo-filesystems are pruned and non-regular files (FIFOs, sockets, devices)
    are skipped, so a broad `search` cannot descend into `/proc` or block opening a pipe.
    """
    for current, directories, files in os.walk(root):
        directories[:] = sorted(
            name
            for name in directories
            if name not in SKIP_DIRECTORIES and os.path.join(current, name) not in SYSTEM_DIRECTORIES
        )
        for filename in sorted(files):
            path = Path(current) / filename
            if _is_regular_file(path):
                yield path


def search(
    pattern: str,
    root: Path,
    *,
    max_matches: int = limits.SEARCH_MAX_MATCHES,
    secrets: Iterable[str] = (),
) -> ToolResult:
    """Find a regular expression in a file or under a directory, returning locations.

    Binary files are skipped rather than decoded: a match inside an ELF section tells
    the model nothing it can act on and costs a screen of mojibake to say it.

    A file is as valid a `root` as a directory. It used to walk one, and `os.walk` over
    a file yields nothing at all, so a pattern plainly present in the file the task had
    just named came back as "no match" -- an inapplicable tool reporting itself as
    absent evidence, which is the one answer a small model cannot argue with. A path
    that is neither says so, for the same reason: "not there" and "nothing in it" lead
    to different next actions.

    The expression is applied per line. A pattern spanning a newline cannot match here
    however well it is written, which is why the tool contract in `agent/prompt.py`
    says line-wise rather than leaving the model to infer it from one empty result.
    """
    try:
        expression = re.compile(pattern)
    except re.error as exc:
        return ToolResult(name="search", ok=False, detail=f"bad pattern: {exc}")

    if root.is_file():
        base, targets = root.parent, iter((root,))
    elif root.is_dir():
        base, targets = root, _files_under(root)
    else:
        return ToolResult(name="search", ok=False, detail=f"no such path: {root}")

    hits: list[str] = []
    truncated = False
    for target in targets:
        relative = target.relative_to(base).as_posix()
        try:
            if _looks_binary(target):
                continue
            with target.open("r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, start=1):
                    if not expression.search(line):
                        continue
                    if len(hits) >= max_matches:
                        truncated = True
                        break
                    hits.append(f"{relative}:{number}: {line.rstrip()[:MATCH_LINE_CHARS]}")
        except OSError:
            continue
        if truncated:
            break

    if not hits:
        return ToolResult(name="search", ok=False, detail=f"no match for {pattern!r} under {root}")

    body = "\n".join(hits)
    if truncated:
        body += f"\n… stopped at {max_matches} matches; narrow the pattern"
    return ToolResult(name="search", ok=True, output=redact(body, secrets), truncated=truncated)
