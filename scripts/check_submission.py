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
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import limits  # noqa: E402
from agent.main import fingerprint  # noqa: E402

ENTRYPOINT = "run.sh"

CRLF = b"\r\n"
"""Named rather than inline so this file survives being edited by tooling that
rewrites its own line endings -- which is the very defect being checked for."""

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
        if name.startswith("/") or ".." in Path(name).parts:
            report.fail(f"unsafe path in archive: {name}")
        parts = set(Path(name).parts)
        if parts & FORBIDDEN_PARTS:
            report.fail(f"archive contains an excluded path: {name}")
        if name.endswith(FORBIDDEN_SUFFIXES):
            report.fail(f"archive contains an excluded file type: {name}")

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

    if not entrypoint.startswith(b"#!"):
        report.fail(f"{ENTRYPOINT} has no shebang")

    return archive


def check_cold_start(archive: zipfile.ZipFile, report: Report, parent: Mapping[str, str]) -> None:
    """Extract into a fresh directory and run the entrypoint from there."""
    shell = shutil.which("sh") or shutil.which("bash")
    if shell is None:
        report.skip("cold start: no POSIX shell on this machine")
        return

    with tempfile.TemporaryDirectory() as tmp:
        unpacked = Path(tmp) / "unpacked"
        archive.extractall(unpacked)

        workspace = Path(tmp) / "workspace"
        workspace.mkdir()

        entry = unpacked / ENTRYPOINT
        entry.chmod(0o755)

        try:
            result = subprocess.run(
                [shell, str(entry), SMOKE_INSTRUCTION],
                cwd=workspace,
                env=smoke_env(parent),
                capture_output=True,
                text=True,
                timeout=SMOKE_TIMEOUT_S,
            )
        except FileNotFoundError as exc:
            report.fail(f"cold start could not launch: {exc}")
            return
        except subprocess.TimeoutExpired:
            report.fail(f"cold start did not finish within {SMOKE_TIMEOUT_S}s")
            return

        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip().splitlines()[-5:]
            report.fail(
                f"cold start exited {result.returncode}: " + " | ".join(tail)
            )
            return

        report.ok(f"cold start exited 0 from a clean directory: {result.stdout.strip()[:200]}")

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
