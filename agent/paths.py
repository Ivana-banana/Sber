"""Which absolute paths the task text names, and which of them may be a workspace.

Two different questions, and conflating them is what the first live baseline caught.

E009 ran the archive against a real model on a package whose image `WORKDIR` is not
`/app`. With no `LOCAL_AGENT_WORKDIR` and no `/app`, the workspace fell back to the
runner's cwd -- which the Harbor contract sets to the agent's own install directory.
The run then wrote its deliverable into the agent's source tree, verified it there, and
reported `solved=True` against a verifier that scored zero.

The first attempt at a fix picked one directory out of the task text and called it the
workspace. Review broke it in a way worth recording, because it is the more general
lesson: **input paths are named many times and the deliverable once**, so a
most-mentioned rule reliably picks the directory the task reads from, and the write of
the answer is refused for being outside it. That is the same zero with a different
trigger, not a fix.

So the two questions are answered separately here:

* `named_paths` -- every absolute path the task mentions. This is the write policy.
  A path the task itself names is part of the task's contract, and refusing to write
  there turns a correct action into a wrong artifact. Note what this does *not* widen:
  file contents, logs and command output are not the task text, so nothing the
  environment says can redirect a write. Only the instruction can, and the instruction
  is the task.
* `workspace_candidates` -- the subset that could plausibly be a directory to explore
  and to resolve relative paths against. Getting this wrong costs tokens and context;
  getting the write policy wrong costs the task.
"""

from __future__ import annotations

import re
from pathlib import Path

FILE_URL = re.compile(r"\bfile://(?:localhost)?(?=/)")
"""`file:///srv/out.txt` names a path, so only the scheme is dropped, not the path."""

URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s,;)\]`'\"]*")
"""Stripped before anything else.

`http://127.0.0.1:8080/tmp/report` is not a path to `/tmp`, but the colon before the
port ends the token and the tail reads as one. Network tasks name a URL in nearly every
instruction, and without this the behaviour even depended on whether a port was given.

It stops at a comma or a closing bracket rather than at whitespace: review found that
`see http://host/docs,/srv/data/out.txt` swallowed the real path that followed, which
loses it from the write policy as well as from the workspace choice.
"""

PATH_TOKEN = re.compile(r"/[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*")

TRAILING = "`'\"),;:!?"
"""Punctuation that ends a sentence, not a path. A leading `.` or `-` is legitimate in
a filename, so only the closing kind is stripped, and `.` only at the very end."""

SCAN_LIMIT_CHARS = 64_000
MAX_PATHS = 64
"""Bounds on untrusted input. The cut is taken at whitespace: cutting mid-path would
invent a shorter path that happens to exist -- observed in review, where a truncated
`/srv/bootstrap` became a vote for `/srv`."""

SYSTEM_TREES: tuple[str, ...] = (
    "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
    "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/lib32", "/usr/lib64", "/usr/libexec",
    "/usr/share", "/usr/include",
    "/usr/local/bin", "/usr/local/sbin", "/usr/local/lib", "/usr/local/share",
    "/proc", "/sys", "/dev", "/run", "/boot",
    "/home", "/etc",
)
"""Trees that belong to the operating system's installation, not to any task.

Deliberately not "everything outside `/app`". The first version of this list banned
`/usr` and `/etc` wholesale, which excluded `/usr/src/app` -- the most common non-`/app`
`WORKDIR` convention in Docker images -- and `/etc/nginx`, while leaving `/tmp`, `/var`
and `/root` selectable. It was inverted against where tasks actually live. What is
listed here is the OS's own binaries, libraries and kernel interfaces; `/home` is here
because no task package ships in someone's home directory, and briefing one into the
model's context would be a leak, not a workspace.
"""

MIN_WORKSPACE_DEPTH = 2
"""`/srv/bootstrap` may be a workspace; `/srv` may not.

A task lives in a directory someone created for it. A bare top-level directory is a
mount point that belongs to the image, and selecting one would hand the write policy
and the environment brief an entire subtree -- `/tmp`, `/var`, `/root` and `/opt` all
reach the workspace this way otherwise.
"""


def named_paths(instruction: str) -> tuple[Path, ...]:
    """Every absolute path the task text names, in order, without duplicates.

    Pure text: no filesystem is touched here, and nothing but the instruction is read.
    """
    text = instruction[:SCAN_LIMIT_CHARS]
    if len(instruction) > SCAN_LIMIT_CHARS:
        # Cut back to the last whitespace of any kind, so a path is never halved into
        # a shorter one that happens to exist. `split()` rather than a membership test
        # on " ": a tab-separated instruction has no space in it at all, and the two
        # guards disagreeing silently emptied the whole list. A head with no whitespace
        # is one unfinished token and contributes nothing.
        pieces = text.rsplit(None, 1)
        text = pieces[0] if len(pieces) == 2 else ""
    text = URL.sub(" ", FILE_URL.sub("", text))

    found: list[Path] = []
    seen: set[str] = set()
    for match in PATH_TOKEN.finditer(text):
        token = match.group().rstrip(TRAILING).rstrip(".")
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        found.append(Path(token))
        if len(found) >= MAX_PATHS:
            break
    return tuple(found)


def _existing_ancestor(path: Path) -> Path | None:
    """The nearest existing directory at or above `path`.

    Walks the whole chain, not one level. "Create the directory and write the report
    into it" is an ordinary phrasing, and a deliverable two levels below anything that
    exists yet would otherwise contribute nothing at all.
    """
    current = path
    while True:
        try:
            if current.is_dir():
                return current
        except OSError:
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _under(path: Path, tree: str) -> bool:
    root = Path(tree)
    return path == root or root in path.parents


def _is_system(*candidates: Path) -> bool:
    """True if any spelling of the path lies in an OS tree.

    Both the literal path and its resolved form are checked, because `/proc` links
    resolve *outward*: `/proc/self/cwd` becomes a real working directory and escapes a
    check made only after `resolve()`. Symlinks pointing *into* a system tree need the
    opposite order, so both are tested.
    """
    return any(_under(path, tree) for path in candidates for tree in SYSTEM_TREES)


def workspace_candidates(instruction: str, agent_dir: Path) -> list[Path]:
    """Directories the task points at that could be explored, most-mentioned first.

    Ranking by mentions is right *here* and wrong as a write policy: the directory a
    task names repeatedly is the one it wants read, which is exactly what a workspace
    is for. Where the answer goes is decided by `named_paths`, not by this.
    """
    try:
        agent_root = agent_dir.resolve()
    except OSError:
        agent_root = agent_dir

    counts: dict[Path, int] = {}
    for path in named_paths(instruction):
        directory = _existing_ancestor(path)
        if directory is None:
            continue
        try:
            resolved = directory.resolve()
        except OSError:
            continue
        if len(resolved.parts) - 1 < MIN_WORKSPACE_DEPTH or _is_system(resolved, path):
            continue
        # Our own tree is never the task -- nor is anything containing it, since `/opt`
        # does not become a workspace by our being installed under it.
        if (
            resolved == agent_root
            or agent_root in resolved.parents
            or resolved in agent_root.parents
        ):
            continue
        counts[resolved] = counts.get(resolved, 0) + 1

    return sorted(counts, key=lambda path: -counts[path])


def writable_paths(instruction: str, agent_dir: Path) -> tuple[Path, ...]:
    """The write policy: paths the task named, minus the ones nothing may write.

    `named_paths` alone was not a policy. Review showed the gap on the running code:
    an instruction naming `/etc/ld.so.preload` made it writable, and one naming the
    agent's own directory made the agent's source writable *during its own run*. The
    task text decides where the answer goes; it does not get to decide that the
    operating system or this program is the answer.
    """
    try:
        agent_root = agent_dir.resolve()
    except OSError:
        agent_root = agent_dir

    allowed: list[Path] = []
    for path in named_paths(instruction):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if _is_system(resolved, path):
            continue
        if resolved == agent_root or agent_root in resolved.parents:
            continue
        allowed.append(path)
    return tuple(allowed)


def permits(target: Path, workspace: Path, permitted: tuple[Path, ...]) -> bool:
    """May the run write to `target`?

    True inside the workspace, and true for a path the task itself named -- exactly
    that file, or anything under a directory it named. Everything else is either a
    mistake or the environment steering the run, and both are refused.
    """
    root = workspace.resolve()
    if target == root or root in target.parents:
        return True
    for path in permitted:
        try:
            named = path.resolve()
        except OSError:
            continue
        if target == named:
            return True
        if named.is_dir() and named in target.parents:
            return True
    return False
