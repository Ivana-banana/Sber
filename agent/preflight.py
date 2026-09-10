"""The environment map the loop gets before it spends a token.

Everything here is answerable by the standard library in milliseconds: where the task
lives, what is in it, which interpreter runs and which utilities exist. Paying a small
model to discover the same facts one tool call at a time is the expensive way to be
wrong about them, and published failure analyses put bad localisation and toolchain
guessing near the top of what ends these runs at zero.

So this module has one output -- a short brief -- and two rules about it. It never
calls the model, and it never grows without bound: the brief is resident context,
charged on every request, so the top of the tree is worth carrying and an inventory of
the whole workspace is not.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agent import limits
from agent.redact import redact, secrets_from_env

PROBED_TOOLS: tuple[str, ...] = (
    "git",
    "rg",
    "grep",
    "sed",
    "awk",
    "find",
    "file",
    "strings",
    "xxd",
    "sqlite3",
    "jq",
    "curl",
    "unzip",
    "tar",
    "patch",
    "diff",
    "openssl",
    "tcpdump",
    "objdump",
    "readelf",
    "nc",
    "gdb",
    "nmap",
    "tshark",
)
"""Utilities worth knowing about before the model guesses.

Chosen because a playbook would reach for them and because their absence changes the
plan rather than one command: no `jq` means JSON is filtered in Python, no `sqlite3`
means the same for databases. Nothing here is required for the agent to run.

`rg`, `patch` and `diff` are here because two independent measurements of the target
image found them present, and each one replaces a worse plan: `rg` a recursive `grep`
over a tree, `patch`/`diff` a hand-written edit. `xxd` and `sqlite3` are here for the
opposite reason -- the same measurements found them absent, and a forensics playbook
reaches for both by reflex, so the brief is cheaper reporting them missing once than
paying for the failed call and the model's recovery from it.

The last seven were added on 2026-08-26 after probing the image directly rather than
reading a report about it (`docs/research/image-substitutions.md`). `tcpdump`,
`objdump`, `readelf` and `nc` are present and worth naming: a model that does not know
`tcpdump` is there will not invent it, and `tcpdump -nn -r` dissects a capture that
`tshark` would have. `gdb`, `nmap` and `tshark` are the three a ctf or forensics
playbook reaches for first and none of them exists here.
"""

SUBSTITUTES: dict[str, str] = {
    "xxd": "od -A x -t x1z -v",
    "sqlite3": "python3, stdlib sqlite3, connect('file:PATH?mode=ro', uri=True)",
    "tshark": "tcpdump -nn -r",
    "gdb": "objdump -d / readelf -Wl / nm / strings (static only)",
    "nmap": "nc -z 127.0.0.1 PORT (loopback only)",
}
"""What to do instead, for the absences that would otherwise end a plan.

Every command here was executed inside `secureintelligent/acp`
(`sha256:1324d7bb…c8a3`) under `--network none` on 2026-08-26 and produced the output
the substitution claims; the protocol is in `docs/research/image-substitutions.md`.
Nothing is listed on a guess: a tool with no verified replacement is reported absent
and nothing more, because a wrong second plan costs more than no second plan.
"""

PROC_NET_TCP: tuple[str, ...] = ("/proc/net/tcp", "/proc/net/tcp6")
"""Where the listening sockets are read from.

Procfs and not a utility, because the target image has neither `ss` nor `nmap`
(`docs/research/image-substitutions.md`). Both files, because a service bound to `::1`
appears only in the second one and a task that runs one is not rare.
"""

_TCP_LISTEN = "0A"
"""The `st` column of a socket the model can actually talk to. Everything else in that
file is a connection someone already made, and naming one as a service buys a step and
a recovery."""

_BACKTICK = re.compile(r"`([^`\n]+)`")
"""Backtick-quoted spans of the task text.

A span may not cross a newline, which is also what makes a fenced code block invisible
here: its opening fence has no content before the newline, so nothing matches inside
it. That is the wanted behaviour -- a fence is an example of the output shape, not a
name to look up.
"""

_SKIPPED_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv"})
"""Directories the literal scan does not descend into. Not secrets and not policy:
each one holds thousands of files that answer no question the task asked."""


@dataclass(frozen=True)
class EnvironmentBrief:
    """What preflight found, in the form the prompt will carry."""

    workspace: Path
    exists: bool
    entries: tuple[str, ...] = ()
    hidden_entries: int = 0
    available: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    python: str = ""
    note: str = ""
    """Why the tree is empty, when it is: `missing`, `unreadable` or nothing."""

    ports: tuple[str, ...] = ()
    """Listening TCP sockets as `address:port`, cheapest fact about a task that runs
    a service and the one the model most reliably gets wrong."""

    located: tuple[tuple[str, str], ...] = ()
    """Backtick literals from the task text that exist in the tree, each with where."""

    secrets: tuple[str, ...] = field(default=(), repr=False)

    def render(self) -> str:
        lines = [f"workspace: {self.workspace}"]
        if self.note:
            lines.append(f"state: {self.note}")
        if self.entries:
            lines.append("tree:")
            lines.extend(f"  {entry}" for entry in self.entries)
        if self.hidden_entries:
            lines.append(f"  … {self.hidden_entries} more entries")
        lines.append(f"python3: {self.python}")
        lines.append(f"tools: {', '.join(self.available[1:]) or 'none of the probed set'}")
        if self.absent:
            lines.append(f"absent: {', '.join(self.absent)}")
            swaps = [f"{name}→{SUBSTITUTES[name]}" for name in self.absent if name in SUBSTITUTES]
            if swaps:
                lines.append(f"instead: {'; '.join(swaps)}")
        if self.ports:
            lines.append(f"listening: {', '.join(self.ports)}")
        if self.located:
            lines.append("quoted:")
            lines.extend(f"  {name} → {where}" for name, where in self.located)

        text = redact("\n".join(lines), self.secrets)
        if len(text) > limits.BRIEF_MAX_CHARS:
            text = text[: limits.BRIEF_MAX_CHARS - 20].rstrip() + "\n  … more"
        return text


def _walk(root: Path, budget: int) -> tuple[list[str], int, str]:
    """Breadth-first tree slice: names only, depth-capped, entry-capped.

    Breadth-first on purpose. The files that identify a task -- `README`, `task.toml`,
    a source tree, an evidence log -- sit at the top, while depth is where a `.git`
    object store or a virtualenv will happily spend the whole budget.

    Only an unreadable *root* ends the walk. A subdirectory we may not enter is marked
    on its own line and stepped over, because the alternative -- what this did before
    -- was to drop the rest of the queue and call the whole workspace unreadable.
    """
    listed: list[str] = []
    skipped = 0
    queue: list[tuple[Path, int]] = [(root, 0)]

    while queue:
        current, depth = queue.pop(0)
        try:
            with os.scandir(current) as scan:
                children = sorted(scan, key=lambda item: (not item.is_dir(), item.name))
        except PermissionError:
            if current == root:
                return listed, skipped, "unreadable"
            # One closed door, not a closed workspace. Returning here threw away every
            # entry still queued behind it and reported the whole tree as unreadable;
            # the directory was already listed by its parent, so marking that line is
            # both cheaper and more honest than losing the map.
            shut = f"{current.relative_to(root).as_posix()}/"
            if shut in listed:
                listed[listed.index(shut)] = f"{shut} (unreadable)"
            continue
        except OSError:
            continue

        for child in children:
            relative = Path(child.path).relative_to(root).as_posix()
            if len(listed) >= budget:
                skipped += 1
                continue
            if child.is_dir():
                listed.append(f"{relative}/")
                if depth + 1 < limits.BRIEF_MAX_DEPTH:
                    queue.append((Path(child.path), depth + 1))
            else:
                listed.append(relative)

    return listed, skipped, ""


def _address(hex_address: str) -> str:
    """Procfs address to something a `curl` line can use.

    The kernel prints the address as host-endian 32-bit words in hex, so on a
    little-endian machine every four bytes are reversed -- `0100007F` is 127.0.0.1 and
    not 1.0.0.127. IPv6 is the same trick four times over.
    """
    try:
        raw = bytes.fromhex(hex_address)
    except ValueError:
        return hex_address
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    if len(raw) == 16:
        words = b"".join(raw[at : at + 4][::-1] for at in range(0, 16, 4))
        return socket.inet_ntop(socket.AF_INET6, words)
    return hex_address


def _listening() -> tuple[str, ...]:
    """Every TCP socket in the LISTEN state, deduplicated and port-ordered.

    Absence of procfs is silence, not a line saying so: a brief that reports "no ports"
    on a kernel that simply does not expose them is a claim we cannot make.
    """
    found: dict[tuple[str, int], None] = {}
    for name in PROC_NET_TCP:
        try:
            content = Path(name).read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        for line in content.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[3] != _TCP_LISTEN:
                continue
            address, _, port = fields[1].rpartition(":")
            try:
                found[(_address(address), int(port, 16))] = None
            except ValueError:
                continue
    ordered = sorted(found, key=lambda socket_: (socket_[1], socket_[0]))
    return tuple(f"{address}:{port}" for address, port in ordered[: limits.BRIEF_MAX_PORTS])


def _quoted(instruction: str, workspace: Path) -> list[str]:
    """The backtick literals worth looking for, in the order the task names them.

    The workspace's own path is dropped. Every task names it -- `/app`,
    `/workspace/review`, `/var/lib/case-42` -- and the only thing a search for it finds
    is the `WORKDIR` and `COPY` lines of the image's own Dockerfile, which answer
    nothing the first line of this brief did not already say.
    """
    here = str(workspace).rstrip("/")
    wanted: list[str] = []
    for match in _BACKTICK.finditer(instruction):
        literal = match.group(1).strip()
        if not 3 <= len(literal) <= limits.BRIEF_LITERAL_MAX_CHARS:
            continue
        if literal.rstrip("/") == here:
            continue
        if literal not in wanted:
            wanted.append(literal)
        if len(wanted) >= limits.BRIEF_MAX_LITERALS:
            break
    return wanted


def _locate(root: Path, wanted: Sequence[str]) -> list[tuple[str, str]]:
    """`grep -rn` for those literals, bounded, in the standard library.

    Bounded rather than complete on purpose: this is orientation paid for once, and the
    loop has a real `search` tool for the question this one cannot answer. Files are
    read whole up to `BRIEF_SCAN_MAX_BYTES` and skipped unread when their head contains
    a NUL, which is the rule `grep` itself uses to call a file binary.

    Only the literals that were found come back. Reporting the misses looked useful for
    one draft and was wrong twice over: `ctf-duplicate-parameter` quotes
    `127.0.0.1:9139`, which the `listening:` line above answers and a "not found" line
    would flatly contradict, and an audit task quotes the field names of the report it
    is being asked to write, which are absent for the least interesting reason there is.
    """
    hits: dict[str, list[str]] = {literal: [] for literal in wanted}
    if not wanted:
        return []

    seen = 0
    bytes_left = limits.BRIEF_SCAN_MAX_FILES * limits.BRIEF_SCAN_MAX_BYTES
    for directory, subdirectories, names in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name not in _SKIPPED_DIRS]
        for name in sorted(names):
            if seen >= limits.BRIEF_SCAN_MAX_FILES:
                break
            if all(len(where) >= limits.BRIEF_LITERAL_MAX_HITS for where in hits.values()):
                break
            path = Path(directory) / name
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
                if bytes_left <= 0:
                    break
                with path.open("rb") as handle:
                    blob = handle.read(min(limits.BRIEF_SCAN_MAX_BYTES, bytes_left))
            except OSError:
                continue
            seen += 1
            bytes_left -= len(blob)
            if b"\x00" in blob[: limits.BINARY_SNIFF_BYTES]:
                continue
            relative = path.relative_to(root).as_posix()
            for number, line in enumerate(blob.decode("utf-8", "replace").splitlines(), start=1):
                for literal in wanted:
                    where = hits[literal]
                    if len(where) < limits.BRIEF_LITERAL_MAX_HITS and literal in line:
                        where.append(f"{relative}:{number}")

    return [(literal, ", ".join(hits[literal])) for literal in wanted if hits[literal]]


def inspect_environment(
    workspace: Path, env: Mapping[str, str], instruction: str = ""
) -> EnvironmentBrief:
    """Look at the workspace and the machine, without touching the model.

    `instruction` is untrusted data and is used here for exactly one thing: the strings
    it puts in backticks become search terms. Nothing it says changes what this
    function does, and what comes back is a list of paths and line numbers from the
    workspace -- the same class of text the tree already carries.
    """
    secrets = secrets_from_env(env)
    python = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    found = {name: bool(shutil.which(name)) for name in PROBED_TOOLS}
    available = ("python3",) + tuple(name for name in PROBED_TOOLS if found[name])
    absent = tuple(name for name in PROBED_TOOLS if not found[name])
    ports = _listening()

    if not workspace.is_dir():
        return EnvironmentBrief(
            workspace=workspace,
            exists=False,
            available=available,
            absent=absent,
            python=python,
            note="missing",
            ports=ports,
            secrets=secrets,
        )

    entries, hidden, note = _walk(workspace, limits.BRIEF_MAX_ENTRIES)
    located = _locate(workspace, _quoted(instruction, workspace))
    return EnvironmentBrief(
        workspace=workspace,
        exists=True,
        entries=tuple(entries),
        hidden_entries=hidden,
        available=available,
        absent=absent,
        python=python,
        note=note,
        ports=ports,
        located=tuple(located),
        secrets=secrets,
    )
