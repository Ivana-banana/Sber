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
import subprocess
import threading
import time
from collections.abc import Iterable, Sequence
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
) -> ToolResult:
    """Run a command with no shell between us and it, bounded in time and size."""
    started = time.monotonic()
    spawn: dict[str, object] = {}
    if os.name == "posix":
        spawn["start_new_session"] = True

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
    `resolve_workspace` is a guess about where the task lives and E009 showed it can be
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


def search(
    pattern: str,
    root: Path,
    *,
    max_matches: int = limits.SEARCH_MAX_MATCHES,
    secrets: Iterable[str] = (),
) -> ToolResult:
    """Find a regular expression under `root`, returning locations, not files.

    Binary files are skipped rather than decoded: a match inside an ELF section tells
    the model nothing it can act on and costs a screen of mojibake to say it.
    """
    try:
        expression = re.compile(pattern)
    except re.error as exc:
        return ToolResult(name="search", ok=False, detail=f"bad pattern: {exc}")

    hits: list[str] = []
    truncated = False
    for current, directories, files in os.walk(root):
        directories[:] = sorted(name for name in directories if name not in SKIP_DIRECTORIES)
        for filename in sorted(files):
            target = Path(current) / filename
            relative = target.relative_to(root).as_posix()
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
        if truncated:
            break

    if not hits:
        return ToolResult(name="search", ok=False, detail=f"no match for {pattern!r} under {root}")

    body = "\n".join(hits)
    if truncated:
        body += f"\n… stopped at {max_matches} matches; narrow the pattern"
    return ToolResult(name="search", ok=True, output=redact(body, secrets), truncated=truncated)
