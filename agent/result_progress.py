"""Detect changes to production source without treating reports as repairs.

The loop needs a narrower fact than ``state.artifacts``: a code-fix run may write a
README, a report, or a test while leaving the application untouched.  This module
keeps that policy in one deep, bounded interface.  A scan that cannot be complete is
explicitly unknown; callers must never interpret an incomplete scan as "no changes".
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

MAX_SOURCE_FILES = 256
MAX_FILE_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024

EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "tests",
        "test",
        "fixtures",
        "logs",
        "reports",
        "artifacts",
        "build",
        "dist",
        "coverage",
    }
)

SOURCE_FILENAMES = frozenset(
    {
        "Dockerfile",
        "Containerfile",
        "Makefile",
        "Rakefile",
        "Gemfile",
        "Procfile",
        "requirements.txt",
        "pyproject.toml",
        "package.json",
        "tsconfig.json",
        "go.mod",
        "Cargo.toml",
        ".env",
    }
)
"""`.env` is here rather than in the suffixes: `Path(".env").suffix` is empty, because
a leading dot names the file, it does not extend it."""

SOURCE_SUFFIXES = frozenset(
    {
        ".asm",
        ".bash",
        ".c",
        ".cc",
        ".cfg",
        ".conf",
        ".cpp",
        ".cs",
        ".css",
        ".env",
        ".go",
        ".h",
        ".hpp",
        ".htm",
        ".html",
        ".ini",
        ".j2",
        ".java",
        ".jinja",
        ".jinja2",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".lua",
        ".php",
        ".pl",
        ".properties",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
"""Templates and config carry fixes as often as `.py` does -- the reviewer's XSS probe
put one in `templates/index.html` and the gate never saw it (F6).  `.md` stays out on
purpose: a report is the thing this module exists to tell apart from a repair.  `.txt`
is in because task data and seeds use it; a report written as `answer.txt` in the
workspace root is the accepted cost, and the gate is a guard, not the verifier."""

TEST_NAME_PREFIX = "test_"
TEST_NAME_MARKERS = ("_test.", ".test.", ".spec.")
"""`test_` counts only where it starts the name.  As a substring it read `latest_events.py`
as a test and left a production module permanently invisible to the gate."""

SHEBANG = b"#!"


@dataclass(frozen=True)
class SourceSnapshot:
    """A bounded digest map captured before the agent starts acting."""

    files: Mapping[str, str]
    known: bool
    reason: str = ""


@dataclass(frozen=True)
class SourceChanges:
    """The source paths that changed, or an explicit unknown result."""

    paths: tuple[str, ...]
    known: bool
    reason: str = ""


def snapshot_sources(workspace: Path) -> SourceSnapshot:
    """Capture bounded digests of production source/config files below ``workspace``."""
    return _scan(workspace)


def changed_sources(workspace: Path, baseline: SourceSnapshot) -> SourceChanges:
    """Compare the current bounded scan to ``baseline``.

    ``known=False`` is deliberately sticky.  An unreadable, oversized, symlinked, or
    over-limit tree cannot prove that no production source changed.
    """
    if not baseline.known:
        return SourceChanges((), False, baseline.reason or "baseline source scan was incomplete")

    current = snapshot_sources(workspace)
    if not current.known:
        return SourceChanges((), False, current.reason or "current source scan was incomplete")

    paths = tuple(
        sorted(
            path
            for path in set(baseline.files) | set(current.files)
            if baseline.files.get(path) != current.files.get(path)
        )
    )
    return SourceChanges(paths, True)


def _scan(workspace: Path) -> SourceSnapshot:
    try:
        root = workspace.resolve()
    except OSError as exc:
        return SourceSnapshot({}, False, f"workspace could not be resolved: {type(exc).__name__}")
    if not root.is_dir():
        return SourceSnapshot({}, False, "workspace is not a directory")

    files: dict[str, str] = {}
    reasons: list[str] = []
    total_bytes = 0
    candidate_count = 0

    def on_error(error: OSError) -> None:
        reasons.append(f"walk error: {type(error).__name__}")

    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False, onerror=on_error):
        current_dir = Path(directory)
        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            if dirname in EXCLUDED_DIRS:
                continue
            candidate = current_dir / dirname
            try:
                if candidate.is_symlink():
                    reasons.append(f"symlink directory skipped: {dirname}")
                    continue
            except OSError:
                reasons.append(f"directory inspection failed: {dirname}")
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in sorted(filenames):
            path = current_dir / filename
            if not _is_source_candidate(path):
                continue
            candidate_count += 1
            if candidate_count > MAX_SOURCE_FILES:
                reasons.append(f"more than {MAX_SOURCE_FILES} source files")
                continue
            try:
                if path.is_symlink():
                    reasons.append(f"symlink file skipped: {filename}")
                    continue
                size = path.stat().st_size
                if size > MAX_FILE_BYTES:
                    reasons.append(f"source file exceeds {MAX_FILE_BYTES} bytes: {filename}")
                    continue
                if total_bytes + size > MAX_TOTAL_BYTES:
                    reasons.append(f"source tree exceeds {MAX_TOTAL_BYTES} bytes")
                    continue
                content = path.read_bytes()
            except OSError:
                reasons.append(f"source file could not be read: {filename}")
                continue
            if len(content) != size:
                reasons.append(f"source file changed while reading: {filename}")
                continue
            total_bytes += size
            relative = path.relative_to(root).as_posix()
            files[relative] = hashlib.sha256(content).hexdigest()

    reason = "; ".join(dict.fromkeys(reasons))
    return SourceSnapshot(files, not reasons, reason)


def _is_source_candidate(path: Path) -> bool:
    filename = path.name
    if filename in SOURCE_FILENAMES:
        return True
    lower = filename.lower()
    if lower.startswith(TEST_NAME_PREFIX) or any(marker in lower for marker in TEST_NAME_MARKERS):
        return False
    suffix = Path(lower).suffix
    if suffix:
        return suffix in SOURCE_SUFFIXES
    return _has_shebang(path)


def _has_shebang(path: Path) -> bool:
    """An extensionless file is source only when it announces an interpreter.

    Both guards are load-bearing: a symlink would be followed here, before the scan's
    own symlink check runs, and `open` on a FIFO left in the workspace would block the
    read -- and with it the whole run -- forever.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with path.open("rb") as handle:
            return handle.read(len(SHEBANG)) == SHEBANG
    except OSError:
        return False
