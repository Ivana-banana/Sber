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
from agent.redact import redact

CHUNK_BYTES = 65536
KILL_GRACE_S = 5.0
SKIP_DIRECTORIES = frozenset({".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"})
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


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill the child and everything it started.

    `sh -c "cmd &"` returns immediately and leaves the background job running; killing
    only the shell leaves that job holding the CPU for the rest of the run. On POSIX
    the child gets its own session, so one `killpg` reaches all of it.
    """
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
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
    what makes it usable as a scrubber: the macro controller hands a generated program
    an environment with the endpoint credentials removed, and an "extend" semantics
    would have left them in place while looking like it had not. `None` keeps the
    parent environment, which is what every existing caller wants.
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
        _kill_tree(process)
        try:
            # Bounded on purpose: a process that survives SIGKILL is stuck in the
            # kernel, and waiting on it forever is the hang we came here to prevent.
            process.wait(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            pass

    for reader in readers:
        reader.join(timeout=1.0)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()

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
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
    except OSError as exc:
        return ToolResult(
            name="write_file",
            ok=False,
            detail=redact(f"{exc.__class__.__name__}: {exc}", secrets),
            duration_s=time.monotonic() - started,
        )

    return ToolResult(
        name="write_file",
        ok=True,
        output=f"wrote {len(content.encode('utf-8'))} bytes to {target}",
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
        return ToolResult(name="read_range", ok=False, detail=f"{exc.__class__.__name__}: {exc}")

    if not collected:
        return ToolResult(
            name="read_range",
            ok=False,
            detail=f"no line {start_line}: file has {last_line} lines",
        )

    body = "".join(collected)
    if truncated:
        body += f"\n… [window cut at line {start_line + len(collected) - 1}] …"
    return ToolResult(name="read_range", ok=True, output=redact(body, secrets), truncated=truncated)


def list_dir(path: Path, *, limit: int = limits.LIST_MAX_ENTRIES) -> ToolResult:
    """List one directory, bounded, directories marked with a trailing slash."""
    try:
        entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name))
    except OSError as exc:
        return ToolResult(name="list_dir", ok=False, detail=f"{exc.__class__.__name__}: {exc}")

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
    return ToolResult(name="list_dir", ok=True, output="\n".join(lines), truncated=truncated)


def _files_under(root: Path) -> Iterable[Path]:
    """Every text-candidate file `search` will open, in a stable order."""
    for current, directories, files in os.walk(root):
        directories[:] = sorted(name for name in directories if name not in SKIP_DIRECTORIES)
        for filename in sorted(files):
            yield Path(current) / filename


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
