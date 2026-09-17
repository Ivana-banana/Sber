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
import shlex
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

from agent import limits, result_progress
from agent.paths import permits, write_refusal
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
    # stdin is /dev/null and not ours: no caller here feeds the child anything, and a
    # command with a forgotten operand -- `grep ERROR` without a file, a bare `cat`,
    # `python3`, `sqlite3` -- would otherwise read the agent's own stdin and wait out
    # the whole tool timeout, which on a file task is the whole task budget. At EOF the
    # same commands answer at once, which is the answer the model can act on.
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
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
            detail=f"refused: {write_refusal(target, root, permitted)}",
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
            detail=f"refused: {write_refusal(target, root, permitted)}",
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

    On a CRLF file an anchor written with "\n" is matched against the line ends the file
    actually has, and the replacement is written back with them, because that anchor is
    what `read_range` handed the model in the first place. The translated form must be
    unique too; when it is not, the refusal says the endings are the reason. A file whose
    lines end with a bare CR is named in the refusal and nothing more: it is rare enough
    that guessing at it is not worth a write.
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
            detail=f"refused: {write_refusal(target, root, permitted)}",
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
    # `read_range` opens a file with universal newlines, so on a CRLF file the model is
    # shown "\n" and copies "\n" back into the anchor, while this comparison is over the
    # raw bytes, where the line ends are "\r\n". Such an anchor could never match, and
    # the refusal below speaks only of indentation -- so the model rewrites indentation
    # for as long as its budget lasts. Translating the anchor is safe only when it is
    # unambiguous, which is the same bar the whole tool holds: the CRLF form must occur
    # exactly once, and neither side may already carry a "\r" of its own.
    crlf_anchor = False
    if found == 0 and "\r\n" in before and "\r" not in old and "\r" not in new:
        as_written = old.replace("\n", "\r\n")
        if before.count(as_written) == 1:
            old, new = as_written, new.replace("\n", "\r\n")
            found, crlf_anchor = 1, True

    if found == 0:
        # Only when the CRLF form is really there: an anchor that is absent either way
        # is not a line-ending problem, and a wrong hint costs a step to disprove.
        crlf_found = before.count(old.replace("\n", "\r\n")) if "\r\n" in before and "\r" not in old else 0
        # A classic-Mac file ends its lines with a lone "\r", which `read_range` also
        # shows as "\n". Named, not translated: it is a rarer file than a CRLF one, and
        # this branch has already refused to touch it.
        bare_cr = "\r" in before and "\r\n" not in before and "\r" not in old and "\n" in old
        cr_found = before.count(old.replace("\n", "\r")) if bare_cr else 0
        if old.startswith("…") or old.endswith("…"):
            # `search` marks a line it cut with "…", and the hit is the text the model
            # has in front of it when it writes the anchor.
            hint = (
                " The `…` is where `search` cut the line for display; it is not in the file. "
                "Read the region with read_range and copy the anchor from there."
            )
        elif crlf_found > 1:
            hint = (
                f" This file's lines end with CRLF, and with \\r\\n between them `old` appears "
                f"{crlf_found} times; extend it with surrounding lines until it is unique."
            )
        elif crlf_found:
            hint = " This file's lines end with CRLF: put \\r\\n between the lines of `old`."
        elif cr_found:
            hint = " This file's lines end with a bare CR: put \\r between the lines of `old`."
        else:
            hint = ""
        return ToolResult(
            name="replace_text",
            ok=False,
            detail=(
                f"`old` does not appear in {target}. Read the file and copy the text "
                f"to replace exactly, including indentation.{hint}"
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
    matched = " (matched with CRLF line endings, which the file keeps)" if crlf_anchor else ""
    return ToolResult(
        name="replace_text",
        ok=True,
        output=f"replaced 1 occurrence at line {line} of {target}{matched}{note}",
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


def _match_window(line: str, at: int) -> str:
    """One match line, bounded and centred on the match rather than on the line's start.

    The cut used to be `line[:MATCH_LINE_CHARS]` with no marker, so a match past column
    200 -- minified JS or HTML, one long JSON string, a line-delimited forensic event
    (300-800 characters is the normal size of one), a base64 blob, a long configuration
    value -- reached the model as a screen of padding with a "here is the line" in front
    of it and nothing in the line. Shifting the window onto the match and marking each
    side that was cut with a "…" costs the same 200 characters and carries the evidence.

    A line that fits is returned exactly as before, marker and all: this is the tool's
    ordinary output, and it must not move. Neither does a line whose match the old window
    already showed -- shifting there costs the head of the line, which is where a line
    says what it is about and what a model copies as an anchor, and buys nothing. Over
    this repository that was 1 433 023 of the 1 450 080 long match lines.
    """
    text = line.rstrip()
    if len(text) <= MATCH_LINE_CHARS:
        return text
    # `rstrip` shortens the text the window is cut from, so a match inside the trailing
    # whitespace can start past its end; clamp rather than trust the index.
    at = max(0, min(at, len(text)))
    if at < MATCH_LINE_CHARS - MATCH_LINE_CHARS // 4:
        start = 0
    else:
        # Clamped so the window is always full and always inside the line; a little
        # context before the match is worth more than centring it exactly.
        start = max(0, min(at - MATCH_LINE_CHARS // 4, len(text) - MATCH_LINE_CHARS))
    head = "…" if start else ""
    tail = "…" if start + MATCH_LINE_CHARS < len(text) else ""
    return f"{head}{text[start : start + MATCH_LINE_CHARS]}{tail}"


def _skipped_note(count: int, compressed: bool) -> str:
    """What to add to `no match` when files were skipped, empty when none were.

    Empty and not "0 files skipped" on purpose: the ordinary answer of this tool is
    compared against itself across runs, and a search that skipped nothing must read
    exactly as it always has.

    A count and one way out, no paths. An ordinary /app holds a database file, a few
    images, a compiled translation and a rotated log, so the note is on the end of every
    fruitless search there; a list of names would make that a paragraph, and the paths
    are one `ls` away for a model that wants them. The gzip clause only when a `.gz` was
    actually among them, for the same reason.
    """
    if not count:
        return ""
    gz = ", or zgrep" if compressed else ""
    return f"; {count} binary/compressed file(s) not searched -- try run_shell: strings <file> | grep{gz}"


def search(
    pattern: str,
    root: Path,
    *,
    max_matches: int = limits.SEARCH_MAX_MATCHES,
    secrets: Iterable[str] = (),
) -> ToolResult:
    """Find a regular expression in a file or under a directory, returning locations.

    Binary files are skipped rather than decoded: a match inside an ELF section tells
    the model nothing it can act on and costs a screen of mojibake to say it. When the
    search then finds nothing, how many files were skipped is said out loud, because
    `no match` over a tree of binaries is a proof of absence the model has no way to
    doubt -- and on a CTF or a forensic image the evidence is usually in exactly those
    files, reachable through `strings` or `zgrep` in the shell.

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
    skipped = 0
    compressed = False
    truncated = False
    for target in targets:
        relative = target.relative_to(base).as_posix()
        try:
            if _looks_binary(target):
                skipped += 1
                compressed = compressed or target.suffix.lower() == ".gz"
                continue
            with target.open("r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, start=1):
                    match = expression.search(line)
                    if match is None:
                        continue
                    if len(hits) >= max_matches:
                        truncated = True
                        break
                    # Redacted before the window is cut, not only after. Redaction is a
                    # substring replacement, so a window that cuts through a key leaves a
                    # prefix that no longer matches the value -- and a prefix of a key is
                    # a key. The match is located again in the redacted line because the
                    # placeholder is a different length; if it no longer matches there,
                    # the placeholder swallowed it and the head of the line is the only
                    # window that is honest about what is left.
                    shown, at = line, match.start()
                    if secrets:
                        shown = redact(line, secrets)
                        if shown != line:
                            moved = expression.search(shown)
                            at = moved.start() if moved is not None else 0
                    hits.append(f"{relative}:{number}: {_match_window(shown, at)}")
        except OSError:
            continue
        if truncated:
            break

    if not hits:
        note = _skipped_note(skipped, compressed)
        return ToolResult(name="search", ok=False, detail=f"no match for {pattern!r} under {root}{note}")

    body = "\n".join(hits)
    if truncated:
        body += f"\n… stopped at {max_matches} matches; narrow the pattern"
    return ToolResult(name="search", ok=True, output=redact(body, secrets), truncated=truncated)


# --- Who else is still running the code this run changed ---------------------
#
# Observation only. Nothing below signals, starts or stops anything: an earlier attempt
# to restart the service before self-verification was reverted for good reasons -- the
# restarted process became the agent's own child and was then protected from the next
# check, a failed restart left the service dead, the matching caught a database and the
# container's entrypoint, and on a forensics task the same code would have re-executed
# the process under investigation. What survives that review is the half that cannot
# break anything: read procfs, say what is running, and let the model decide.
#
# The fact worth saying is narrow. Measured over three code-fix episodes on the 30B
# stand: the model edited the handler, ran the project's own tests, and they passed --
# against a server started at container entry without reload, which was still serving
# the pre-edit module. The run reported itself solved and the grader, which restarts
# the service itself, scored zero. A check that could not have exercised the change is
# not evidence, and the only way to know is to compare when a process started with when
# the file changed.
#
# Two rules decide whether a process is running changed code, and both are narrow on
# purpose, because the cost of a wrong yes is a refused `finish` on a correct run:
#
#   1. the command line names the changed file itself;
#   2. the command line names an *entry point* -- an existing source file, reached
#      either as a path or as a `module:attr` / `package.module` token -- and the
#      changed file lives under that entry point's directory. That is the shape of
#      `uvicorn main:app` started from `/app` with `routers/auth.py` edited: nothing in
#      the command line mentions the router, but `main.py` sits beside it.
#
# A process is deliberately *not* matched because its command line names a directory
# that holds a changed file. That rule looked reasonable and matched `mysqld
# --datadir=/app` -- a database that has never imported a line of the application.

PROC_ROOT = Path("/proc")
"""Where the process table lives. Every function below takes `proc_root` and falls back
to this, so a test can hand over a synthetic table instead of the machine's own."""

TCP_LISTEN = "0A"
"""`/proc/net/tcp` state column for a listening socket."""

DELETED_MARKER = " (deleted)"
"""What procfs appends to a link whose target is gone. Such a path names nothing."""

CONFIG_SUFFIXES = frozenset(
    {
        ".bash", ".cfg", ".conf", ".ini", ".json", ".properties",
        ".sh", ".sql", ".toml", ".xml", ".yaml", ".yml",
    }
)
"""Data and configuration, which are not the code a process is executing.

`agent/result_progress.py` counts these as production *files* and it is right to: a
repair may well be a changed setting. They are excluded here for a different question.
Nothing imports `config.yml`, so a running process cannot be "the one serving" it, and
treating the container's entrypoint script or a database's config file as an entry
point is exactly how the reverted attempt came to match `mysqld` and `redis-server`.
"""

PRODUCTION_SUFFIXES = frozenset(result_progress.SOURCE_SUFFIXES) - CONFIG_SUFFIXES
"""Source suffixes a running program can be executing. Derived, never re-typed."""

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
"""Directories too broad to be anyone's project root. Every file on the machine is
under `/`, so a rule that accepts one of these accepts everything."""


@dataclass(frozen=True)
class ProcessInfo:
    """One running process, as much of it as procfs would tell us."""

    pid: int
    ppid: int
    argv: tuple[str, ...]
    cwd: Path | None = None
    """`None` when the link is unreadable, or points at a directory that was removed."""

    started_at: float = 0.0
    """Wall-clock epoch seconds when the process was exec'd, `0.0` when unknown."""


@dataclass(frozen=True)
class ProcessTable:
    """Everything one bounded walk of procfs saw, and whether it saw all of it."""

    processes: tuple[ProcessInfo, ...] = ()
    complete: bool = True
    reason: str = ""


@dataclass(frozen=True)
class ServingProcess:
    """A process whose command line says it is running code this run has changed."""

    pid: int
    argv: tuple[str, ...]
    """At most `limits.SERVICE_SCAN_ARGV_TOKENS` tokens, redacted and single-lined."""

    cwd: Path | None = None
    started_at: float = 0.0
    """Wall-clock epoch seconds, `0.0` when procfs did not say. A caller comparing this
    against a file time must treat `0.0` as unknown and conclude nothing."""

    ports: tuple[int, ...] = ()
    """Listening TCP ports this pid owns. Empty means *unknown*, never "owns none":
    `/proc/<pid>/fd` is closed to us for another user's process."""

    reason: str = ""
    """Why this process was matched, in one short English phrase, for the trace."""

    command: str = ""
    """The full command line, shell-quoted, when every token is safe to show; "" when
    any token looks like a credential or a URL with one, or when procfs gave none. The
    restart hint quotes it verbatim: on the measured 30B stand the model stopped the
    service it was told about and never started it again, because "run the same command
    again" left it to reconstruct the command from the entrypoint (16.09)."""

    @property
    def label(self) -> str:
        """The command line as a person would name the service: `uvicorn main:app`."""
        if not self.argv:
            return f"pid {self.pid}"
        head = Path(self.argv[0]).name or self.argv[0]
        return " ".join((head, *self.argv[1:]))


@dataclass(frozen=True)
class ServiceScan:
    """What one scan found. `known` is about the scan, never about the machine.

    `known=False` means the walk could not be completed -- no procfs, a permission
    refusal, the process cap or the time budget. The processes it did find are still
    real and a caller may act on them; what it must not do is read an empty result from
    an incomplete scan as "nothing is running the changed code".
    """

    processes: tuple[ServingProcess, ...] = ()
    known: bool = True
    reason: str = ""


def _label_tokens(argv: Sequence[str]) -> list[str]:
    """The program and the tokens that name what it runs -- never an option value.

    argv[0] plus up to two tokens shaped like an entry point (a colon, a dot or a slash
    in them), skipping anything that looks like a URL or a credential
    (`scheme://user:pw@host`). A separate-token password after `--requirepass` has no
    such shape and is not carried either (critic, 16.09).
    """
    if not argv:
        return []
    kept = [argv[0]]
    for token in argv[1:]:
        if len(kept) >= limits.SERVICE_SCAN_ARGV_TOKENS:
            break
        if token.startswith("-") or "://" in token or "@" in token:
            continue
        if ":" in token or "." in token or "/" in token:
            kept.append(token)
    return kept


def _safe_token(value: str, secrets: Sequence[str]) -> str:
    """One argv token, fit to travel into an observation and a trace.

    An option *value* is dropped, not redacted: `--password=...`, `--api-key=...` and
    `--dsn=postgres://user:pw@host/db` are ordinary ways to start a service, and the
    agent's own `redact` only knows the credentials it was given, not the machine's.
    What the model needs from a command line is which program is running and what it
    was pointed at, and that survives the cut.
    """
    head, separator, _ = value.partition("=")
    if value.startswith("-") and separator:
        value = f"{head}=…"
    return redact(_one_line(value), secrets)[: limits.SERVICE_SCAN_TOKEN_CHARS]


def _one_line(value: str) -> str:
    """Collapse control characters so one token cannot break a one-event-per-line log."""
    out: list[str] = []
    for char in value:
        code = ord(char)
        if char in "\n\r\t":
            out.append(" ")
        elif code < 32 or code == 127:
            out.append(".")
        else:
            out.append(char)
    return "".join(out)


def _is_production_source(path: Path) -> bool:
    """Whether this path is application code rather than a test, a report or a setting.

    The repo already defines production source in `agent/result_progress.py`; this
    mirrors that definition -- excluded directories, test-marked names -- and subtracts
    configuration and data, which no process imports.
    """
    name = path.name.lower()
    if any(marker in name for marker in result_progress.TEST_NAME_MARKERS):
        return False
    if any(part in result_progress.EXCLUDED_DIRS for part in path.parts):
        return False
    return path.suffix.lower() in PRODUCTION_SUFFIXES


def production_sources(paths: Iterable[Path]) -> tuple[Path, ...]:
    """Existing production source files among `paths`, resolved and deduplicated."""
    found: dict[Path, None] = {}
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in found or not _is_production_source(resolved):
            continue
        try:
            if not resolved.is_file():
                continue
        except OSError:
            continue
        found[resolved] = None
    return tuple(found)


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
    """One `/proc/<pid>` directory, or `None` for anything that cannot be a service.

    A zombie and a kernel thread both have an empty `cmdline`, and neither holds a port
    or executes code; a process that exits while we are reading it looks the same as a
    permission refusal, and both mean the same thing here -- not a candidate.
    """
    try:
        raw = (entry / "cmdline").read_bytes()
    except OSError:
        return None
    # Non-UTF-8 argv is ordinary: a command line is bytes, and a mojibake filename is
    # exactly the kind of thing a forensics fixture contains. Replacing keeps the token
    # printable; it can only fail to match, never match the wrong file.
    argv = tuple(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part)
    if not argv:
        return None

    ppid, started_at = 0, 0.0
    try:
        stat_line = (entry / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        stat_line = ""
    # Field 2 is the command name, parenthesised, and may itself contain spaces and
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
        cwd = None if not target or target.endswith(DELETED_MARKER) else Path(target)

    return ProcessInfo(pid=pid, ppid=ppid, argv=argv, cwd=cwd, started_at=started_at)


def process_table(
    *,
    proc_root: Path | None = None,
    max_processes: int = limits.SERVICE_SCAN_MAX_PROCESSES,
    deadline: float | None = None,
) -> ProcessTable:
    """Every process procfs will describe, in pid order, bounded by count and clock.

    `deadline` is a `time.monotonic()` instant. Both bounds report themselves through
    `complete`: a partial walk is never allowed to look like a complete one.
    """
    root = PROC_ROOT if proc_root is None else proc_root
    try:
        names = sorted((name for name in os.listdir(root) if name.isdigit()), key=int)
    except OSError as exc:
        return ProcessTable((), False, f"procfs is not readable: {type(exc).__name__}")
    boot_time, ticks = _boot_time(root), _clock_ticks()
    found: list[ProcessInfo] = []
    complete, reason = True, ""
    for index, name in enumerate(names):
        if index >= max_processes:
            complete, reason = False, f"scan stopped at {max_processes} processes"
            break
        if deadline is not None and index % 16 == 0 and time.monotonic() > deadline:
            complete, reason = False, "scan stopped on its time budget"
            break
        info = _read_process(root / name, int(name), boot_time, ticks)
        if info is not None:
            found.append(info)
    return ProcessTable(tuple(found), complete, reason)


def _own_tree(pid: int, table: Sequence[ProcessInfo]) -> frozenset[int]:
    """`pid`, every parent above it and every process below it.

    The agent must never report itself, the runner that launched it, or a server it
    started as the thing serving stale code: the first two are what is doing the
    checking, and the third already has the change.
    """
    parents = {info.pid: info.ppid for info in table}
    children: dict[int, list[int]] = {}
    for info in table:
        children.setdefault(info.ppid, []).append(info.pid)

    found = {pid}
    current = parents.get(pid, 0)
    while current > 0 and current not in found:
        found.add(current)
        current = parents.get(current, 0)

    queue = list(children.get(pid, ()))
    while queue:
        child = queue.pop()
        if child in found:
            continue
        found.add(child)
        queue.extend(children.get(child, ()))
    return frozenset(found)


def _holding_directories(targets: Sequence[Path]) -> dict[Path, Path]:
    """Every directory that can be called the project root of a changed file.

    Maps the directory to the changed file that put it there, so a match can say which
    edit it is talking about. Bounded by depth and filtered by name, because "the
    directory holding the change" is `/` for every file on the machine.
    """
    found: dict[Path, Path] = {}
    for target in targets:
        for depth, parent in enumerate(target.parents):
            if depth >= limits.SERVICE_SCAN_MAX_DEPTH:
                break
            if str(parent) in GENERIC_DIRECTORIES:
                continue
            found.setdefault(parent, target)
    return found


def _token_paths(token: str, cwd: Path | None, wanted_names: frozenset[str]) -> list[Path]:
    """Paths an argv token could be naming, resolved the way the process would see them.

    `main:app`, `--app-dir=/srv/api`, `app.main:app` and `./server.js` are all ordinary
    ways to name the code a server loads, so each token is reduced to its path-shaped
    part and offered in both file and dotted-module form.

    The name filter in the middle is not a shortcut, it is the cost bound: resolving a
    path costs syscalls, a command line is mostly options, ports and log paths, and the
    whole scan is a fraction of a second for every process on the machine. A token
    survives only if it could still match -- it names a changed file, or it ends in a
    source suffix and might therefore be an entry point.
    """
    if not token or (token.startswith("-") and "=" not in token):
        return []
    head = token.split("=", 1)[1] if token.startswith("-") else token
    head = head.split(":", 1)[0].strip().strip("'\"")
    if not head or head in {".", ".."}:
        return []
    if ":" not in token and "/" not in head and "." not in head:
        # A bare word (`run`, `worker`, `test`) is a subcommand, not an entry point;
        # letting it resolve against `<word>.py` made `flask run` match a stray
        # `run.py` (critic, 16.09). Entry points carry a colon, a dot or a slash.
        return []

    shapes = [head]
    if "/" not in head and not head.startswith("."):
        # `main:app`, `app.main:app` and `-m app` name a module, not a file. Both module
        # shapes are offered: a module is a file, a package is a directory with an
        # `__init__.py`. Without this the most common service line in the public set --
        # `uvicorn main:app` from the application root -- matches nothing at all.
        dotted = head.replace(".", "/")
        shapes.append(f"{dotted}.py")
        shapes.append(f"{dotted}/__init__.py")

    targets: list[Path] = []
    for shape in shapes:
        candidate = Path(shape)
        named = candidate.name in wanted_names
        if not named and candidate.suffix.lower() not in PRODUCTION_SUFFIXES:
            continue
        if not candidate.is_absolute():
            if cwd is None:
                continue
            candidate = cwd / shape
        try:
            # Symlinks and `..` are resolved on both sides of every comparison: procfs
            # hands back a fully resolved cwd and the changed paths are resolved too, so
            # an argv written against a symlinked project root still matches.
            targets.append(candidate.resolve())
        except OSError:
            targets.append(candidate)
    return targets


def _is_entry_point(target: Path) -> bool:
    """Whether an existing path a command line names could be the code it runs.

    A source file, and nothing else. An extension-less program qualified in the
    reverted attempt and that is what made `/usr/local/bin/redis-server` and
    `/usr/sbin/mysqld` look like entry points of the application whose directory they
    happened to sit near.
    """
    if target.suffix.lower() not in PRODUCTION_SUFFIXES:
        return False
    try:
        return target.is_file()
    except OSError:
        return False


def _autoreloads(argv: Sequence[str]) -> bool:
    """Whether the command line says the server watches its own files for changes."""
    if any(token == "--reload" or token.startswith("--reload-") for token in argv):
        return True
    return "runserver" in argv and "--noreload" not in argv


def _option_value(argv: Sequence[str], name: str) -> str:
    """The value of `--name VALUE` or `--name=VALUE`, or "" when absent."""
    for index, token in enumerate(argv):
        if token == name and index + 1 < len(argv):
            return argv[index + 1]
        if token.startswith(name + "="):
            return token.split("=", 1)[1]
    return ""


def _serves(
    process: ProcessInfo,
    targets: frozenset[Path],
    directories: Mapping[Path, Path],
    wanted_names: frozenset[str],
) -> str:
    """Why this process would be running changed code, or "" if there is no reason to."""
    # Every name below comes from the filesystem, so it is single-lined before it can
    # reach a trace: a file called "auth\n[verify] ok.py" is a legal file name and this
    # text is rendered one event per line.
    argv = list(process.argv[: limits.SERVICE_SCAN_MAX_ARGV_SCANNED])
    if _autoreloads(argv):
        # A reloader picks the edit up by itself; its supervising process keeps its
        # start time forever and would be a permanent false positive.
        return ""
    bases: list[Path | None] = [process.cwd]
    app_dir = _option_value(argv, "--app-dir")
    if app_dir:
        bases.append(Path(app_dir) if Path(app_dir).is_absolute() or process.cwd is None
                     else process.cwd / app_dir)
    entries: list[Path] = []
    for token in argv:
        for base in bases:
            for candidate in _token_paths(token, base, wanted_names):
                if candidate in targets:
                    return _one_line(f"argv names {candidate.name}")
                entries.append(candidate)
    for entry in entries:
        changed = directories.get(entry.parent)
        if changed is None or not _is_entry_point(entry):
            continue
        return _one_line(f"loads {entry.name} from {entry.parent}, where {changed.name} changed")
    return ""


def _listen_table(*, proc_root: Path | None = None) -> dict[int, int]:
    """Socket inode -> listening port, for every TCP socket in LISTEN.

    `agent/preflight.py` has a reporting-only twin of this that renders `address:port`
    for the environment brief. This one keeps the inode, which is the only link back to
    the process holding the port. Read line by line and capped: in a busy container
    these files hold one line per established connection.
    """
    root = PROC_ROOT if proc_root is None else proc_root
    ports: dict[int, int] = {}
    for name in ("tcp", "tcp6"):
        read = 0
        try:
            with (root / "net" / name).open("r", encoding="ascii", errors="replace") as handle:
                for line in handle:
                    read += 1
                    if read > limits.SERVICE_SCAN_MAX_SOCKET_LINES:
                        break
                    fields = line.split()
                    if len(fields) < 10 or fields[3] != TCP_LISTEN:
                        continue
                    _, _, port = fields[1].rpartition(":")
                    try:
                        ports[int(fields[9])] = int(port, 16)
                    except ValueError:
                        continue
        except OSError:
            continue
    return ports


def _socket_inodes(pid: int, *, proc_root: Path | None = None) -> frozenset[int]:
    """Socket inodes held by `pid`, or an empty set when the directory is closed to us.

    Empty is indistinguishable from "holds no socket", so callers must read it as
    unknown -- never as evidence that a process owns no port.
    """
    root = PROC_ROOT if proc_root is None else proc_root
    entry = root / str(pid) / "fd"
    try:
        names = os.listdir(entry)
    except OSError:
        return frozenset()
    inodes: set[int] = set()
    for name in names[: limits.SERVICE_SCAN_MAX_FDS]:
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


def serving_processes(
    modified: Sequence[Path],
    *,
    proc_root: Path | None = None,
    secrets: Sequence[str] = (),
    budget_s: float = limits.SERVICE_SCAN_BUDGET_S,
    max_processes: int = limits.SERVICE_SCAN_MAX_PROCESSES,
) -> ServiceScan:
    """Processes whose command line says they are running one of `modified`.

    Read-only and bounded: no signal is sent, no process is started, and the whole walk
    obeys `budget_s`. Every failure degrades to `known=False`, never to an exception --
    this runs inside the loop's finish gate and a raise there would cost the task.
    """
    deadline = time.monotonic() + max(0.0, budget_s)
    targets = production_sources(modified)[: limits.SERVICE_SCAN_MAX_MODIFIED]
    if not targets:
        return ServiceScan((), True, "no modified production source")

    table = process_table(proc_root=proc_root, max_processes=max_processes, deadline=deadline)
    if not table.processes:
        return ServiceScan((), table.complete, table.reason or "no process could be read")

    protected = _own_tree(os.getpid(), table.processes)
    wanted = frozenset(target.name for target in targets)
    directories = _holding_directories(targets)
    resolved = frozenset(targets)

    matched: list[tuple[ProcessInfo, str]] = []
    complete, reason = table.complete, table.reason
    for info in table.processes:
        if info.pid <= 1 or info.pid in protected:
            continue
        if time.monotonic() > deadline:
            complete, reason = False, "scan stopped on its time budget"
            break
        found = _serves(info, resolved, directories, wanted)
        if found:
            matched.append((info, found))
            if len(matched) >= limits.SERVICE_SCAN_MAX_MATCHES:
                break

    ports = _listen_table(proc_root=proc_root) if matched else {}
    found_processes: list[ServingProcess] = []
    for info, found in matched:
        inodes = _socket_inodes(info.pid, proc_root=proc_root) if ports else frozenset()
        found_processes.append(
            ServingProcess(
                pid=info.pid,
                argv=tuple(
                    _safe_token(token, secrets)
                    for token in _label_tokens(info.argv)
                ),
                cwd=info.cwd,
                started_at=info.started_at,
                ports=tuple(sorted({port for inode, port in ports.items() if inode in inodes})),
                reason=found,
                command=_relaunch_command(info.argv, secrets),
            )
        )
    return ServiceScan(tuple(found_processes), complete, reason)


_SECRET_OPTION_MARKERS = ("pass", "secret", "token", "key", "dsn", "url", "auth", "cred")


def _relaunch_command(argv: Sequence[str], secrets: Sequence[str]) -> str:
    """The command line as one shell-quotable string, or "" when it must not travel.

    Refused whenever a token carries a URL with credentials or follows an option whose
    name suggests a secret (`--requirepass hunter2`, `--dsn=...`): the model's prompt
    and the trace must not become the place a service password is written down. The
    agent's own endpoint credential is redacted as everywhere else.
    """
    if not argv or len(argv) > limits.SERVICE_SCAN_MAX_ARGV_SCANNED:
        return ""
    previous = ""
    for token in argv:
        lowered = token.lower()
        if "://" in token or "@" in token:
            return ""
        if previous.startswith("-") and any(marker in previous.lower() for marker in _SECRET_OPTION_MARKERS):
            return ""
        if token.startswith("-") and "=" in token and any(
            marker in lowered.split("=", 1)[0] for marker in _SECRET_OPTION_MARKERS
        ):
            return ""
        if "\n" in token or "\x00" in token:
            return ""
        previous = token
    quoted = " ".join(shlex.quote(token) for token in argv)
    return redact(quoted, secrets)[: limits.SERVICE_SCAN_TOKEN_CHARS * 8]
