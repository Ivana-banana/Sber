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
import shutil
import sys
from collections.abc import Mapping
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

        text = redact("\n".join(lines), self.secrets)
        if len(text) > limits.BRIEF_MAX_CHARS:
            text = text[: limits.BRIEF_MAX_CHARS - 20].rstrip() + "\n  … more"
        return text


def _walk(root: Path, budget: int) -> tuple[list[str], int, str]:
    """Breadth-first tree slice: names only, depth-capped, entry-capped.

    Breadth-first on purpose. The files that identify a task -- `README`, `task.toml`,
    a source tree, an evidence log -- sit at the top, while depth is where a `.git`
    object store or a virtualenv will happily spend the whole budget.
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
            return listed, skipped, "unreadable"
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


def inspect_environment(workspace: Path, env: Mapping[str, str]) -> EnvironmentBrief:
    """Look at the workspace and the machine, without touching the model."""
    secrets = secrets_from_env(env)
    python = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    found = {name: bool(shutil.which(name)) for name in PROBED_TOOLS}
    available = ("python3",) + tuple(name for name in PROBED_TOOLS if found[name])
    absent = tuple(name for name in PROBED_TOOLS if not found[name])

    if not workspace.is_dir():
        return EnvironmentBrief(
            workspace=workspace,
            exists=False,
            available=available,
            absent=absent,
            python=python,
            note="missing",
            secrets=secrets,
        )

    entries, hidden, note = _walk(workspace, limits.BRIEF_MAX_ENTRIES)
    return EnvironmentBrief(
        workspace=workspace,
        exists=True,
        entries=tuple(entries),
        hidden_entries=hidden,
        available=available,
        absent=absent,
        python=python,
        note=note,
        secrets=secrets,
    )
