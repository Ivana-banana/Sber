"""Build the archive that goes to the organisers.

    python scripts/build_submission.py [--out build/submission.zip]

Two properties matter more than convenience here.

**Allowlist, not denylist.** Only `run.sh` and `agent/` are packed. A denylist has to
anticipate every way a virtualenv, a cache, a log or a `.env` can appear in the tree,
and it is wrong exactly once before it leaks a secret or blows the size limit.

**Reproducible bytes.** Timestamps are frozen, entries are sorted and permissions are
written explicitly, so the same commit always produces the same SHA-256. Without that
the hash recorded next to a checkpoint proves nothing about what was uploaded.

The permission bit is not cosmetic. On Windows there is no executable bit on disk, so
a naively written `run.sh` arrives in the archive as mode 0644 and the runner cannot
start it. It is set here on purpose, and `scripts/check_submission.py` verifies it
from the built file rather than trusting this code.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import limits  # noqa: E402

INCLUDE_FILES = ("run.sh",)
INCLUDE_TREES = ("agent",)

SKIP_DIRS = frozenset({"__pycache__", ".git", ".venv", ".pytest_cache", ".ruff_cache"})
SKIP_SUFFIXES = (".pyc", ".pyo", ".log")

EXECUTABLE = frozenset({"run.sh"})

FROZEN_TIME = (1980, 1, 1, 0, 0, 0)
"""The earliest timestamp the zip format can represent. Any fixed value works; this
one is unmistakably a placeholder rather than a real build time."""


def collect(root: Path) -> list[tuple[Path, str]]:
    """Return (absolute path, archive name) pairs, sorted for reproducibility."""
    found: list[tuple[Path, str]] = []

    for name in INCLUDE_FILES:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"required file is missing: {name}")
        found.append((path, name))

    for tree in INCLUDE_TREES:
        base = root / tree
        if not base.is_dir():
            raise FileNotFoundError(f"required directory is missing: {tree}")
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if SKIP_DIRS & set(path.relative_to(root).parts):
                continue
            if path.suffix in SKIP_SUFFIXES:
                continue
            found.append((path, path.relative_to(root).as_posix()))

    return sorted(found, key=lambda pair: pair[1])


def build(root: Path, out: Path) -> tuple[int, str]:
    """Write the archive and return (size in bytes, sha256 hex)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    entries = collect(root)

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, name in entries:
            info = zipfile.ZipInfo(name, date_time=FROZEN_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if name in EXECUTABLE else 0o644
            info.external_attr = (mode << 16) | 0o100000
            archive.writestr(info, path.read_bytes())

    data = out.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the submission archive.")
    parser.add_argument("--out", default="build/submission.zip", type=Path)
    args = parser.parse_args()

    out = args.out if args.out.is_absolute() else ROOT / args.out
    size, digest = build(ROOT, out)

    print(f"archive: {out}")
    print(f"size:    {size} bytes ({size / 1024 / 1024:.2f} MiB)")
    print(f"sha256:  {digest}")

    if size > limits.MAX_SUBMISSION_BYTES:
        print(
            f"FAIL: over the {limits.MAX_SUBMISSION_BYTES} byte limit",
            file=sys.stderr,
        )
        return 1

    print("\nnow verify the built file, not this run:")
    print(f"  python scripts/check_submission.py {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
