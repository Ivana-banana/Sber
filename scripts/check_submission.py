"""Preflight for the archive that goes to the organisers.

    python scripts/check_submission.py build/submission.zip

Checks the **file on disk**, never the source tree it was supposedly built from. The
pattern is lifted from the AIOS hackathon, where the same distinction mattered: the
artefact can be built by an older version of the builder, copied between machines,
truncated in transit or edited by hand, and none of that is visible from the code
that would have produced it.

Three outcomes, kept separate on purpose:

* **FAIL** -- the archive cannot work. Exit code 1.
* **WARN** -- it can work, but something here has cost a run before.
* **SKIP** -- the check could not run in this environment. Never silently a pass:
  a skipped cold start is exactly the gap that produces a surprise zero.

What this script does NOT prove: that the agent works in `secureintelligent/acp` with
the network off. That needs the target image and is tracked as a separate gate.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import limits  # noqa: E402
from agent.main import fingerprint  # noqa: E402

ENTRYPOINT = "run.sh"

UNIX_HOST = 3
"""Host system an entry must declare before its mode counts as a Unix mode.

Spelled out here rather than imported from the builder on purpose. This file checks the
file on disk, and a checker that learned the number from the code that wrote it would
agree with a wrong builder instead of catching one."""

CONTRACT_LABEL = "cold start contract"
DIAGNOSTIC_LABEL = "cold start diagnostic"
"""Two different questions, so two different labels. See `check_cold_start`."""

CRLF = b"\r\n"
BOM = b"\xef\xbb\xbf"
"""Named rather than inline so this file survives being edited by tooling that
rewrites its own line endings -- which is the very defect being checked for."""

UTF8_NAME_FLAG = 0x800
"""APPNOTE 4.4.4 bit 11 (\"language encoding flag\"): the central directory says a
name is UTF-8 only when this is set. Unset, the bytes are whatever the writer's
local codepage was, and a non-ASCII name decodes differently on every reader."""

DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:")

FORBIDDEN_PARTS = frozenset({".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".env"})
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".log", ".key", ".pem")

SMOKE_INSTRUCTION = 'smoke "test" with $pecial \\chars and a newline\nsecond line'
"""Deliberately hostile. The contract is that arbitrary task text survives the shell,
and quoting bugs in an entrypoint are invisible until a real instruction has a quote
in it."""

SMOKE_ENV = {
    "LOCAL_AGENT_MODEL": "preflight-fake-model",
    "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
    "OPENAI_API_KEY": "preflight-not-a-real-key",
    "AGENT_TIMEOUT_S": "30",
}
"""Port 9 is the discard port: reachable syntax, nothing listening. A cold start must
survive a dead endpoint, because that is what a broken evaluation node looks like."""

PASSTHROUGH_ENV = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    # Windows only, and only so the local run stays honest. Without SYSTEMROOT and
    # PROGRAMDATA the Microsoft Store python shim expands `%SystemDrive%` literally
    # and creates a directory tree in the working directory, which would show up
    # forever as a false "the agent littered the workspace" warning.
    "SYSTEMROOT",
    "COMSPEC",
    "PROGRAMDATA",
    "TEMP",
)
"""Everything else is dropped, so the smoke cannot inherit a real key or a
developer's configuration. A shell with no PATH cannot resolve `dirname` or
`python3`, which would test this script rather than the archive."""

SMOKE_TIMEOUT_S = 60


def smoke_env(parent: Mapping[str, str]) -> dict[str, str]:
    env = {name: parent[name] for name in PASSTHROUGH_ENV if name in parent}
    env.update(SMOKE_ENV)
    return env


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.skipped: list[str] = []
        self.passed: list[str] = []

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def skip(self, message: str) -> None:
        self.skipped.append(message)

    def ok(self, message: str) -> None:
        self.passed.append(message)


def _unsafe_path_reason(name: str) -> str | None:
    """Reason a member's name is unsafe to ever hand to a filesystem call, or None.

    Names in a zip's central directory are POSIX paths regardless of the platform
    that built or is checking the archive (APPNOTE 4.4.17.1). Parsing one with the
    platform `Path` -- `WindowsPath` on this checker's own machine -- treats a
    leading drive letter as harmless, when a POSIX extractor (or a *nix-built archive
    read on Linux, where nothing strips it first) would see one hostile path segment.
    `PurePosixPath` reads the name the way the format actually specifies it. The
    backslash check exists for the same reason even though `zipfile` itself already
    normalises `os.sep` out of every name it parses on a Windows machine: this
    function has to give one verdict regardless of which platform runs the check.
    """
    if "\\" in name:
        return "contains a backslash"
    if DRIVE_LETTER_RE.match(name):
        return "starts with a drive letter"
    if name.startswith("/"):
        return "is an absolute path"
    if ".." in PurePosixPath(name).parts:
        return "contains a parent-directory segment"
    return None


def _entry_type_reason(info: zipfile.ZipInfo) -> str | None:
    """Reason a Unix-origin member's declared type is unacceptable, or None.

    Only `S_IFREG` and `S_IFDIR` are ever legitimate in this archive. A symlink lets a
    member point anywhere the extracting process can read or -- through a mode applied
    later -- write; nothing else (device, fifo, socket) belongs in a code submission.
    A DOS-origin entry carries no Unix type bits at all and is judged elsewhere.
    """
    if info.create_system != UNIX_HOST:
        return None
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        return "declares a symlink"
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        return f"declares an unexpected type (mode {mode:o})"
    return None


def check_archive(path: Path, report: Report) -> zipfile.ZipFile | None:
    """Structural checks that do not need the contents extracted."""
    size = path.stat().st_size
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    report.ok(f"size {size} bytes ({size / 1024 / 1024:.2f} MiB), sha256 {digest}")

    if size > limits.MAX_SUBMISSION_BYTES:
        report.fail(f"archive is {size} bytes, over the {limits.MAX_SUBMISSION_BYTES} byte limit")

    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        report.fail(f"not a readable zip: {exc}")
        return None

    broken = archive.testzip()
    if broken is not None:
        report.fail(f"corrupt entry in archive: {broken}")
        return None
    report.ok("all entries decompress")

    names = archive.namelist()
    if not names:
        report.fail("archive is empty")
        return archive

    for name in names:
        unsafe = _unsafe_path_reason(name)
        if unsafe is not None:
            report.fail(f"unsafe path in archive: {name} ({unsafe})")
        parts = set(PurePosixPath(name).parts)
        if parts & FORBIDDEN_PARTS:
            report.fail(f"archive contains an excluded path: {name}")
        if name.endswith(FORBIDDEN_SUFFIXES):
            report.fail(f"archive contains an excluded file type: {name}")

    for info in archive.infolist():
        # `archive.getinfo(name)` resolves duplicate names through `NameToInfo`, which
        # collapses to one entry -- a shadowed duplicate would never be inspected. This
        # loop walks the central directory directly instead, so every member is seen.
        type_reason = _entry_type_reason(info)
        if type_reason is not None:
            report.fail(f"archive member {info.filename} {type_reason}")
        if info.file_size > limits.MAX_SUBMISSION_BYTES:
            report.fail(
                f"archive member is {info.file_size} bytes uncompressed, over the "
                f"{limits.MAX_SUBMISSION_BYTES} byte limit even though the archive is "
                f"small: {info.filename}"
            )
        if any(ord(ch) > 127 for ch in info.filename) and not info.flag_bits & UTF8_NAME_FLAG:
            report.fail(f"non-ASCII name without the UTF-8 flag (0x800): {info.filename}")

    if ENTRYPOINT not in names:
        # The one open question in the packaging contract: the README requires the
        # entrypoint at the root, while the published sample archive wraps everything
        # in a directory. Guessing is not allowed, so a wrapped layout is reported as
        # what it is rather than quietly accepted.
        wrapped = [n for n in names if n.endswith("/" + ENTRYPOINT)]
        if wrapped:
            report.fail(
                f"{ENTRYPOINT} is nested ({wrapped[0]}), not at the archive root; "
                "the normative layout is unresolved, see docs/decisions.md"
            )
        else:
            report.fail(f"{ENTRYPOINT} is missing from the archive")
        return archive

    mode = archive.getinfo(ENTRYPOINT).external_attr >> 16
    if not mode & 0o111:
        report.fail(f"{ENTRYPOINT} is not executable in the archive (mode {mode:o})")
    else:
        report.ok(f"{ENTRYPOINT} is present at the root, mode {mode:o}")

    entrypoint = archive.read(ENTRYPOINT)

    # A Windows checkout with core.autocrlf=true rewrites the entrypoint to CRLF, the
    # builder packs whatever it finds, and Linux then reports `/bin/sh^M: bad
    # interpreter`. Checked here rather than trusting .gitattributes, which only helps
    # the machines that have it.
    if CRLF in entrypoint:
        report.fail(f"{ENTRYPOINT} has CRLF line endings; the target shell will not start it")
    elif b"\r" in entrypoint:
        # Old Mac Classic style: a bare CR with no following LF. `CRLF in entrypoint`
        # does not see this, and it is just as fatal to the target shell.
        report.fail(f"{ENTRYPOINT} has a lone CR line ending; the target shell will not start it")

    if entrypoint.startswith(BOM):
        report.fail(f"{ENTRYPOINT} starts with a UTF-8 BOM before the shebang; the target shell will not start it")
    elif not entrypoint.startswith(b"#!"):
        report.fail(f"{ENTRYPOINT} has no shebang")

    return archive


def apply_declared_modes(archive: zipfile.ZipFile, dest: Path) -> dict[str, int]:
    """Apply the permissions the archive declares, and return what was applied.

    `extractall` restores no permissions at all, on any platform, so an extracted tree
    says nothing on its own about how the archive was packed. This reproduces what a
    mode-restoring extractor does instead: take the mode from the central directory,
    and only where the host byte says it is a Unix mode. An archive built on Windows
    without that byte set declares DOS, gets nothing applied here, and will not run --
    which is the case worth catching.

    Real `unzip` is the ground truth this approximates. Neither settles what the
    organisers' runner does; only a run in the target image can.
    """
    applied: dict[str, int] = {}
    for info in archive.infolist():
        if info.create_system != UNIX_HOST:
            continue
        mode = (info.external_attr >> 16) & 0o777
        if not mode:
            continue
        (dest / info.filename).chmod(mode)
        applied[info.filename] = mode
    return applied


def launch(
    argv: list[str], workspace: Path, parent: Mapping[str, str]
) -> tuple[subprocess.CompletedProcess[str] | None, str]:
    """Run the entrypoint once. Exactly one of (result, error message) is set."""
    try:
        result = subprocess.run(
            argv,
            cwd=workspace,
            env=smoke_env(parent),
            capture_output=True,
            text=True,
            timeout=SMOKE_TIMEOUT_S,
        )
    except OSError as exc:
        # Not just a missing file: launching a file whose executable bit did not
        # survive extraction arrives here as PermissionError, and that is the failure
        # this check exists for.
        return None, f"could not launch: {exc}"
    except subprocess.TimeoutExpired:
        return None, f"did not finish within {SMOKE_TIMEOUT_S}s"

    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-5:]
        return None, f"exited {result.returncode}: " + " | ".join(tail)
    return result, ""


def check_cold_start(archive: zipfile.ZipFile, report: Report, parent: Mapping[str, str]) -> None:
    """Extract into a fresh directory and run the entrypoint the way the runner will.

    Two runs, and the difference between them is the whole point.

    The **contract** run launches the extracted `run.sh` directly with nothing repaired
    first, on whatever mode survived extraction. It is the only check that proves the
    archive carries a usable executable bit.

    The **diagnostic** run forces the mode and goes through `sh` explicitly. It answers
    a different question -- whether the script itself is sound -- and runs only when the
    contract run could not, or did not, succeed.

    These used to be one run: the diagnostic one, under the contract's name. It chmod'ed
    the entrypoint before launching it and so repaired the exact defect it was meant to
    detect, which is how an archive declaring a DOS host byte passed preflight.
    """
    with tempfile.TemporaryDirectory() as tmp:
        unpacked = Path(tmp) / "unpacked"
        archive.extractall(unpacked)
        applied = apply_declared_modes(archive, unpacked)

        entry = unpacked / ENTRYPOINT
        declared = applied.get(ENTRYPOINT, 0)

        workspace = Path(tmp) / "contract"
        workspace.mkdir()
        result = None

        if not declared & 0o111:
            host = archive.getinfo(ENTRYPOINT).create_system
            report.fail(
                f"{CONTRACT_LABEL}: extraction leaves {ENTRYPOINT} without an executable "
                f"mode (host {host}, applied {declared:o}); the runner cannot start it"
            )
        elif os.name == "nt":
            # Windows has no execve for a shell script, so the launch would fail here
            # for a reason that has nothing to do with the archive.
            report.skip(
                f"{CONTRACT_LABEL}: Windows cannot execute a shell script directly, so "
                f"only the declared mode {declared:o} was checked; rerun this on Linux"
            )
        else:
            # An absolute path, not a literal `./run.sh`, because the working directory
            # belongs to the task rather than to us -- see run.sh. Either way the kernel
            # still demands the executable bit, which is the property under test.
            result, error = launch([str(entry), SMOKE_INSTRUCTION], workspace, parent)
            if error:
                report.fail(f"{CONTRACT_LABEL}: {ENTRYPOINT} {error}")
            else:
                report.ok(
                    f"{CONTRACT_LABEL}: {ENTRYPOINT} ran from a clean directory on the "
                    f"mode the archive declared ({declared:o}), nothing repaired first"
                )

        if result is None:
            shell = shutil.which("sh") or shutil.which("bash")
            if shell is None:
                report.skip(f"{DIAGNOSTIC_LABEL}: no POSIX shell on this machine")
                return

            workspace = Path(tmp) / "diagnostic"
            workspace.mkdir()
            entry.chmod(0o755)

            result, error = launch([shell, str(entry), SMOKE_INSTRUCTION], workspace, parent)
            if error:
                report.fail(f"{DIAGNOSTIC_LABEL}: `sh {ENTRYPOINT}` {error}")
                return
            report.ok(
                f"{DIAGNOSTIC_LABEL}: `sh {ENTRYPOINT}` works once the mode is forced, "
                "so the script itself is sound"
            )

        # Exit 0 alone does not prove the entrypoint carried the task text. The agent
        # prints a digest of what it received; if quoting mangled it, this is where
        # that shows up instead of on a real task.
        expected = f"{len(SMOKE_INSTRUCTION)}:{fingerprint(SMOKE_INSTRUCTION)}"
        if f"instruction={expected}" in result.stdout:
            report.ok("the instruction survived the shell intact, quotes and newline included")
        else:
            report.fail(
                f"the instruction did not arrive intact: expected {expected}, "
                f"got {result.stdout.strip()[:200]!r}"
            )

        # The workspace is the task's directory, not ours. An agent that scatters
        # files there before doing any work will collide with whatever the task
        # checks for.
        litter = sorted(p.name for p in workspace.iterdir())
        if litter:
            report.warn(f"cold start left files in the workspace: {', '.join(litter)}")


def check(path: Path, parent_env: Mapping[str, str]) -> Report:
    report = Report()
    if not path.is_file():
        report.fail(f"no such file: {path}")
        return report

    archive = check_archive(path, report)
    if archive is None or report.failures:
        # A structurally broken archive tells us nothing useful when executed, and
        # running an entrypoint we already know is wrong just muddies the output.
        if archive is not None:
            report.skip("cold start: skipped because the archive already failed")
        return report

    check_cold_start(archive, report, parent_env)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a built submission archive.")
    parser.add_argument("archive", type=Path, nargs="?", default=Path("build/submission.zip"))
    args = parser.parse_args()

    path = args.archive if args.archive.is_absolute() else ROOT / args.archive
    report = check(path, os.environ)

    for message in report.passed:
        print(f"PASS  {message}")
    for message in report.skipped:
        print(f"SKIP  {message}")
    for message in report.warnings:
        print(f"WARN  {message}")
    for message in report.failures:
        print(f"FAIL  {message}", file=sys.stderr)

    if report.failures:
        print(f"\n{len(report.failures)} blocking problem(s)", file=sys.stderr)
        return 1

    print("\nno blocking problems.")
    if report.skipped:
        print("skipped checks are not passes: rerun where they can execute.")
    print("this does not prove offline operation in secureintelligent/acp.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
