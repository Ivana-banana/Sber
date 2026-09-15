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

import http.client
import json
import os
import re
import shutil
import signal
import socket
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


def _python_compile_note(data: bytes, target: Path) -> str:
    """A short note if `data` written to a `.py` path does not compile, else empty.

    From bytes, not the decoded string: the tokenizer then handles a BOM and a
    `# coding:` line exactly as `import` does, where `compile(str)` would reject a BOM
    file that imports fine. `compile` parses without executing, so it is safe on
    untrusted source. The except is broad on purpose -- a pathological file can raise
    RecursionError or MemoryError, not only SyntaxError, and the tool dispatcher has no
    catch-all above it, so a raise here would crash the whole run to zero.
    """
    if target.suffix.lower() != ".py":
        return ""
    try:
        compile(data, str(target), "exec")
    except SyntaxError as exc:
        where = "" if exc.lineno is None else f" (line {exc.lineno})"
        return f"; the file does not compile as Python: {exc.msg}{where}"
    except (ValueError, RecursionError, MemoryError) as exc:
        return f"; the file could not be compile-checked as Python: {type(exc).__name__}"
    return ""


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

    written = f"wrote {len(data)} bytes to {target}"
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

    # A byte count reads as success even when the write broke the file. A .py source
    # that no longer compiles is the shape that let a code-fix run overwrite a module
    # with a stub, fail the app's import, and never learn why -- so say so, the way the
    # JSON case above does. `compile` parses without executing, so it is safe on
    # untrusted source.
    written += _python_compile_note(data, target)

    return ToolResult(
        name="write_file",
        ok=True,
        output=written,
        duration_s=time.monotonic() - started,
    )


def write_json(
    path: Path,
    data: object,
    *,
    workspace: Path,
    permitted: tuple[Path, ...] = (),
    secrets: Iterable[str] = (),
) -> ToolResult:
    """Write `data` as a JSON deliverable, serialising it here so the bytes are valid.

    The model hands over the deliverable as a JSON value, not as a string it has to
    escape inside another string. That escaping is exactly where a small model breaks:
    a bug-bounty report whose `evidence` quotes the vulnerable code
    (`... WHERE username = '{req.username}'`) has to survive being a JSON string nested
    inside the action's own JSON, and E-BASE-r1 measured the model getting the finding
    right, losing it to one unescaped quote, being told the file was invalid, and
    emptying its own analysis to `{"findings": []}` to make the parse error go away.
    Serialising on our side removes the nested-escaping step: the value the model
    already produces correctly is written verbatim as valid JSON.

    Confinement is `write_file`'s: the workspace and the paths the task named.
    """
    try:
        root = workspace.resolve()
        target = Path(path)
        if not target.is_absolute():
            target = root / target
        target = target.resolve()
    except OSError as exc:
        return ToolResult(name="write_json", ok=False, detail=f"bad path: {exc}")

    if not permits(target, root, permitted):
        return ToolResult(
            name="write_json",
            ok=False,
            detail=f"refused: {target} is outside the workspace {root} and unnamed by the task",
        )

    # The value is normally already parsed (the action arrived as JSON). A model that
    # still stringifies it is met halfway: a JSON string is parsed once and re-emitted,
    # which is the very escaping burden this tool exists to remove.
    value = data
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            return ToolResult(
                name="write_json", ok=False, detail=f"data is a string but not valid JSON: {exc}"
            )
    if value is None:
        return ToolResult(
            name="write_json", ok=False, detail="write_json needs a data value: the JSON to write"
        )
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        return ToolResult(name="write_json", ok=False, detail=f"data is not JSON-serialisable: {exc}")

    payload = text.encode("utf-8")
    started = time.monotonic()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            handle.write(payload)
    except OSError as exc:
        return ToolResult(
            name="write_json",
            ok=False,
            detail=redact(f"{exc.__class__.__name__}: {exc}", secrets),
            duration_s=time.monotonic() - started,
        )

    return ToolResult(
        name="write_json",
        ok=True,
        output=f"wrote {len(payload)} bytes of JSON to {target}",
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
    # replace_text is the tool the docstring steers a code-fix toward, so an indentation
    # error introduced in `new` breaks the module exactly as a bad write_file would --
    # report it the same way rather than answer only "replaced 1 occurrence".
    note = _python_compile_note(replacement, target)
    return ToolResult(
        name="replace_text",
        ok=True,
        output=f"replaced 1 occurrence at line {line} of {target}{note}",
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


# --- The machine's own processes and ports ----------------------------------
#
# Not tools the model can call: `agent/prompt.py` names every callable tool and this
# section is absent from that list. These are the primitives `agent/verify.py` needs to
# see whether the service under test is still running the code the run has just
# changed. Everything here reads procfs and degrades to "unknown" -- never to an
# exception and never to a guess -- when a file is missing or a permission is refused,
# because a wrong answer would either kill the wrong process or declare a blind check
# green.

PROC_ROOT = Path("/proc")
"""Where the process table lives. Passed explicitly by tests; never hardcoded below."""

PROC_POLL_S = 0.05
"""How often a bounded wait re-reads procfs. Short: these waits are seconds, not minutes."""

TCP_LISTEN = "0A"
"""`/proc/net/tcp` state column for a listening socket."""

SIGNAL_GRACE_S = 3.0
"""Default time a process is given to leave after SIGTERM before SIGKILL."""

KILL_WAIT_S = 1.0
"""Time allowed after SIGKILL. A process that survives it is stuck in the kernel."""

PROBE_READ_BYTES = 256
"""How much of a probe response body is drained. The status line is the whole signal."""

_DETACHED: list[subprocess.Popen[bytes]] = []
"""Handles of processes started by `start_detached`, kept only so the interpreter does
not warn about a child nobody waited for. Nothing ever reads this list: the point of a
detached restart is that the service outlives us."""


@dataclass(frozen=True)
class ProcessInfo:
    """One running process, as much of it as procfs would tell us."""

    pid: int
    ppid: int
    argv: tuple[str, ...]
    cwd: Path | None
    """`None` when the link is unreadable (another user's process) or points nowhere."""

    started_at: float = 0.0
    """Wall-clock epoch seconds when the process was exec'd, `0.0` when unknown."""


@dataclass(frozen=True)
class ListeningSocket:
    """A TCP socket in LISTEN, with the inode that ties it back to a process."""

    address: str
    port: int
    inode: int


@dataclass(frozen=True)
class HttpProbe:
    """What one plain GET got back, split into the three facts a caller must not mix."""

    connected: bool
    """Whether the TCP connection was established at all."""

    spoke_http: bool
    """Whether the answer parsed as HTTP. False for a non-HTTP port or a silent one."""

    status: int | None = None
    error: str = ""


def _clock_ticks() -> float:
    try:
        ticks = os.sysconf("SC_CLK_TCK")
    except (AttributeError, OSError, ValueError):
        return 100.0
    return float(ticks) if ticks and ticks > 0 else 100.0


def _boot_time(proc_root: Path) -> float:
    """Epoch second the kernel booted, or `0.0` if procfs does not say."""
    try:
        content = (proc_root / "stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return 0.0
    for line in content.splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return 0.0
    return 0.0


def _read_process(entry: Path, pid: int, boot_time: float, ticks: float) -> ProcessInfo | None:
    """One `/proc/<pid>` directory, or `None` for anything we must not act on.

    A zombie and a kernel thread both have an empty `cmdline`, and neither can be
    restarted or hold a port; a process that exits while we are reading it looks the
    same as a permission refusal, and both mean the same thing here -- not a candidate.
    """
    try:
        raw = (entry / "cmdline").read_bytes()
    except OSError:
        return None
    argv = tuple(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part)
    if not argv:
        return None

    ppid, started_at = 0, 0.0
    try:
        stat_line = (entry / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        stat_line = ""
    # The command name is field 2, parenthesised, and may itself contain spaces and
    # parentheses -- so the tail is taken after the *last* ')' rather than by splitting.
    close = stat_line.rfind(")")
    if close != -1:
        fields = stat_line[close + 1 :].split()
        if len(fields) >= 20:
            if fields[0] == "Z":
                return None
            try:
                ppid = int(fields[1])
                if boot_time:
                    started_at = boot_time + float(fields[19]) / ticks
            except ValueError:
                ppid, started_at = 0, 0.0

    cwd: Path | None
    try:
        target = os.readlink(entry / "cwd")
    except OSError:
        cwd = None
    else:
        # procfs appends this marker when the directory has been removed; such a path
        # is not somewhere anything can be restarted.
        cwd = None if not target or target.endswith(" (deleted)") else Path(target)

    return ProcessInfo(pid=pid, ppid=ppid, argv=argv, cwd=cwd, started_at=started_at)


def process_table(*, proc_root: Path | None = None) -> tuple[ProcessInfo, ...]:
    """Every process procfs will describe, in pid order. Empty when there is no procfs."""
    root = PROC_ROOT if proc_root is None else proc_root
    try:
        names = [name for name in os.listdir(root) if name.isdigit()]
    except OSError:
        return ()
    boot_time, ticks = _boot_time(root), _clock_ticks()
    found: list[ProcessInfo] = []
    for name in sorted(names, key=int):
        info = _read_process(root / name, int(name), boot_time, ticks)
        if info is not None:
            found.append(info)
    return tuple(found)


def process_alive(pid: int, *, proc_root: Path | None = None) -> bool:
    """Whether `pid` is still running. A zombie is not: it holds no port and no code."""
    root = PROC_ROOT if proc_root is None else proc_root
    entry = root / str(pid)
    try:
        stat_line = (entry / "stat").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False
    except OSError:
        # The directory exists but is closed to us; being unable to look is not proof
        # that it left.
        return entry.exists()
    close = stat_line.rfind(")")
    fields = stat_line[close + 1 :].split() if close != -1 else []
    return not (fields and fields[0] == "Z")


def process_environ(pid: int, *, proc_root: Path | None = None) -> dict[str, str] | None:
    """The environment a process was started with, or `None` if it cannot be read.

    Callers restart a service with exactly this environment, so it is returned whole --
    a service whose database URL or secret key went missing on restart would fail for a
    reason the run did not cause. It is data for `execve`, never for a log: nothing in
    the agent prints it, and `None` rather than `{}` is returned for an unreadable or
    empty entry so a caller cannot restart something into a stripped environment by
    accident.
    """
    root = PROC_ROOT if proc_root is None else proc_root
    try:
        raw = (root / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    env: dict[str, str] = {}
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        name, separator, value = chunk.decode("utf-8", "replace").partition("=")
        if separator and name:
            env[name] = value
    return env or None


def process_ancestors(pid: int, table: Sequence[ProcessInfo]) -> frozenset[int]:
    """Every parent above `pid`, following ppid until it reaches init or a cycle."""
    parents = {info.pid: info.ppid for info in table}
    found: set[int] = set()
    current = parents.get(pid, 0)
    while current > 0 and current not in found:
        found.add(current)
        current = parents.get(current, 0)
    return frozenset(found)


def process_descendants(pid: int, table: Sequence[ProcessInfo]) -> frozenset[int]:
    """Every process started by `pid`, at any depth."""
    children: dict[int, list[int]] = {}
    for info in table:
        children.setdefault(info.ppid, []).append(info.pid)
    found: set[int] = set()
    queue = list(children.get(pid, ()))
    while queue:
        current = queue.pop()
        if current in found or current == pid:
            continue
        found.add(current)
        queue.extend(children.get(current, ()))
    return frozenset(found)


def terminate_tree(
    pid: int,
    table: Sequence[ProcessInfo],
    *,
    grace_s: float = SIGNAL_GRACE_S,
    proc_root: Path | None = None,
) -> str:
    """Stop `pid` and everything it started. Returns "" on success, else why not.

    Signals are sent to named pids, never to a process group. A service started by the
    container's entrypoint shares its group with PID 1 and with the entrypoint's own
    `tail`, so one `killpg` would take the container down with the service -- which is
    both a destructive action outside what the task asked for and an instant zero.

    Worker processes are included because a master that re-execs while a worker still
    holds the listening socket cannot bind it again; the caller restarts the master,
    which starts its own workers.
    """
    if pid <= 1:
        return "refusing to signal pid 1 or lower"
    # Belt and braces: the caller already excludes its own tree, but this is the one
    # destructive primitive in the module and a mistake in a caller must not be able to
    # make the agent kill itself or the runner above it.
    mine = os.getpid()
    protected = {mine, *process_ancestors(mine, table)}
    if pid in protected:
        return "refusing to signal this process or one of its parents"
    targets = [
        target
        for target in (pid, *sorted(process_descendants(pid, table)))
        if target > 1 and target not in protected
    ]
    for number, wait_s in ((signal.SIGTERM, grace_s), (signal.SIGKILL, KILL_WAIT_S)):
        alive = [target for target in targets if process_alive(target, proc_root=proc_root)]
        if not alive:
            return ""
        for target in alive:
            try:
                os.kill(target, number)
            except ProcessLookupError:
                continue
            except PermissionError:
                return f"pid {target} may not be signalled by this process"
            except OSError as exc:
                return f"pid {target} could not be signalled: {type(exc).__name__}"
        deadline = time.monotonic() + max(0.0, wait_s)
        while time.monotonic() < deadline:
            if not any(process_alive(target, proc_root=proc_root) for target in targets):
                return ""
            time.sleep(PROC_POLL_S)
    left = [target for target in targets if process_alive(target, proc_root=proc_root)]
    return f"{len(left)} process(es) survived SIGKILL" if left else ""


def executable_exists(argv0: str, env: Mapping[str, str] | None = None) -> bool:
    """Whether `argv0` can still be exec'd, resolved the way it was resolved at spawn.

    Checked *before* the old process is killed. A restart that terminates a service and
    then discovers the binary is gone leaves the machine worse than it found it.
    """
    if not argv0:
        return False
    path = None if env is None else env.get("PATH")
    try:
        return shutil.which(argv0, path=path) is not None
    except (OSError, ValueError):
        return False


def start_detached(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> tuple[int | None, str]:
    """Start a process that outlives this one. Returns its pid, or `None` and a reason.

    Its own session, so nothing in our process group reaches it, and its output goes to
    `/dev/null`: a restarted service inherits no pipe of ours, which would otherwise let
    it block on a full buffer or interleave its log into the agent's telemetry stream.
    """
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=None if env is None else dict(env),
            start_new_session=os.name == "posix",
        )
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    _DETACHED.append(process)
    return process.pid, ""


def _address(hex_address: str) -> str:
    """A procfs hex address as text. Host-endian words, so every four bytes reverse."""
    try:
        raw = bytes.fromhex(hex_address)
    except ValueError:
        return hex_address
    try:
        if len(raw) == 4:
            return socket.inet_ntop(socket.AF_INET, raw[::-1])
        if len(raw) == 16:
            words = b"".join(raw[at : at + 4][::-1] for at in range(0, 16, 4))
            return socket.inet_ntop(socket.AF_INET6, words)
    except (OSError, ValueError):
        return hex_address
    return hex_address


def listening_sockets(*, proc_root: Path | None = None) -> tuple[ListeningSocket, ...]:
    """Every TCP socket in LISTEN, with its inode, port-ordered.

    `agent/preflight.py` has a reporting-only twin of this: it renders `address:port`
    strings for the environment brief and caps them for the prompt. This one keeps the
    inode, which is the only link back to the process holding the port, and is not
    capped, because a service is not allowed to become invisible here by running on a
    machine that also listens on eight other ports.
    """
    root = PROC_ROOT if proc_root is None else proc_root
    found: dict[tuple[str, int, int], None] = {}
    for name in ("tcp", "tcp6"):
        try:
            content = (root / "net" / name).read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        for line in content.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 10 or fields[3] != TCP_LISTEN:
                continue
            address, _, port = fields[1].rpartition(":")
            try:
                found[(_address(address), int(port, 16), int(fields[9]))] = None
            except ValueError:
                continue
    ordered = sorted(found, key=lambda entry: (entry[1], entry[0]))
    return tuple(ListeningSocket(address, port, inode) for address, port, inode in ordered)


def socket_inodes(pid: int, *, proc_root: Path | None = None) -> frozenset[int]:
    """Socket inodes held by `pid`. Empty when the descriptor directory is closed to us,
    which is indistinguishable from a process holding no socket -- so callers must treat
    an empty set as "unknown", never as "owns no port"."""
    root = PROC_ROOT if proc_root is None else proc_root
    entry = root / str(pid) / "fd"
    try:
        names = os.listdir(entry)
    except OSError:
        return frozenset()
    inodes: set[int] = set()
    for name in names:
        try:
            target = os.readlink(entry / name)
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            try:
                inodes.add(int(target[len("socket:[") : -1]))
            except ValueError:
                continue
    return frozenset(inodes)


def port_open(address: str, port: int, *, timeout_s: float) -> bool:
    """Whether a TCP connection to this address is accepted right now."""
    try:
        with socket.create_connection((address, port), timeout=max(0.01, timeout_s)):
            return True
    except (OSError, ValueError):
        return False


def http_probe(address: str, port: int, *, path: str = "/", timeout_s: float = 2.0) -> HttpProbe:
    """One plain GET, reduced to: did it connect, did it speak HTTP, what did it say.

    The three facts are kept apart because they lead to different conclusions. A refused
    connection on a port that was listening a moment ago is a dead service; an answer
    that does not parse as HTTP is a port we have no business judging; a status is the
    only case where the service's own opinion of itself is readable.
    """
    connection = http.client.HTTPConnection(address, port, timeout=max(0.05, timeout_s))
    try:
        try:
            connection.connect()
        except (OSError, ValueError) as exc:
            return HttpProbe(connected=False, spoke_http=False, error=type(exc).__name__)
        try:
            connection.request("GET", path, headers={"Accept": "*/*", "Connection": "close"})
            response = connection.getresponse()
            status = response.status
            response.read(PROBE_READ_BYTES)
        except http.client.HTTPException as exc:
            # RemoteDisconnected and BadStatusLine land here: the port is open but it is
            # not an HTTP server, or it hung up without answering.
            return HttpProbe(connected=True, spoke_http=False, error=type(exc).__name__)
        except (OSError, ValueError) as exc:
            return HttpProbe(connected=True, spoke_http=False, error=type(exc).__name__)
    finally:
        try:
            connection.close()
        except OSError:
            pass
    return HttpProbe(connected=True, spoke_http=True, status=status)
