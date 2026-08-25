"""Packaging is the one defect class that zeroes all fifteen tasks at once.

Everything here works on a built archive, never on the source tree, because that is
the difference the check is for.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from agent import limits
from scripts.build_submission import ROOT, build
from scripts.check_submission import check

SMOKE_PARENT_ENV: dict[str, str] = {}
"""Deliberately empty in most tests: the cold start is exercised separately, and an
empty parent makes it report SKIP or FAIL rather than depending on the developer's
machine."""


@pytest.fixture(scope="module")
def archive(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("build") / "submission.zip"
    build(ROOT, out)
    return out


# --- the builder ------------------------------------------------------------


def test_build_is_byte_reproducible(tmp_path: Path) -> None:
    first = tmp_path / "a.zip"
    second = tmp_path / "b.zip"
    build(ROOT, first)
    build(ROOT, second)

    assert hashlib.sha256(first.read_bytes()).hexdigest() == (
        hashlib.sha256(second.read_bytes()).hexdigest()
    ), "a hash recorded next to a checkpoint has to identify the uploaded bytes"


def test_archive_holds_only_what_the_allowlist_names(archive: Path) -> None:
    names = zipfile.ZipFile(archive).namelist()

    assert "run.sh" in names
    assert any(name.startswith("agent/") for name in names)

    for name in names:
        top = name.split("/")[0]
        assert top in {"run.sh", "agent"}, f"unexpected entry: {name}"


def test_caches_and_compiled_files_never_ship(archive: Path) -> None:
    names = zipfile.ZipFile(archive).namelist()
    assert not [n for n in names if "__pycache__" in n or n.endswith((".pyc", ".pyo"))]


def test_entrypoint_is_executable_inside_the_archive(archive: Path) -> None:
    # Windows has no executable bit on disk, so this can only come from the builder
    # setting it. Getting it wrong means the runner cannot start us at all.
    mode = zipfile.ZipFile(archive).getinfo("run.sh").external_attr >> 16
    assert mode & 0o111, f"run.sh mode is {mode:o}"


def test_archive_fits_the_limit(archive: Path) -> None:
    assert archive.stat().st_size <= limits.MAX_SUBMISSION_BYTES


def test_missing_source_file_is_an_error_not_an_empty_archive(tmp_path: Path) -> None:
    empty = tmp_path / "src"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        build(empty, tmp_path / "out.zip")


# --- the checker ------------------------------------------------------------


def repack(source: Path, target: Path, *, transform) -> Path:
    """Rewrite an archive entry by entry so a single property can be broken."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w") as dst:
        for info in src.infolist():
            new_info, data = transform(info, src.read(info.filename))
            if new_info is None:
                continue
            dst.writestr(new_info, data)
    return target


def test_a_good_archive_has_no_blocking_problems(archive: Path) -> None:
    import os

    report = check(archive, os.environ)
    assert report.failures == []
    assert any("run.sh is present at the root" in message for message in report.passed)


def test_cold_start_reports_its_own_result(archive: Path) -> None:
    import os

    report = check(archive, os.environ)
    started = [m for m in report.passed if "cold start" in m]
    skipped = [m for m in report.skipped if "cold start" in m]

    assert started or skipped, "the cold start must say what happened, pass or skip"
    assert not (started and skipped)


def test_missing_file_is_reported(tmp_path: Path) -> None:
    report = check(tmp_path / "nope.zip", SMOKE_PARENT_ENV)
    assert report.failures


def test_a_truncated_archive_fails(archive: Path, tmp_path: Path) -> None:
    broken = tmp_path / "truncated.zip"
    broken.write_bytes(archive.read_bytes()[: archive.stat().st_size // 2])

    report = check(broken, SMOKE_PARENT_ENV)
    assert report.failures


def test_a_nested_entrypoint_fails_rather_than_being_accepted(
    archive: Path, tmp_path: Path
) -> None:
    # The published sample archive wraps everything in a directory while the README
    # requires the root. The layout question is open, so the checker must refuse to
    # decide it silently.
    def wrap(info, data):
        new = zipfile.ZipInfo("submission/" + info.filename, date_time=info.date_time)
        new.external_attr = info.external_attr
        return new, data

    nested = repack(archive, tmp_path / "nested.zip", transform=wrap)
    report = check(nested, SMOKE_PARENT_ENV)

    assert any("nested" in message for message in report.failures)


def test_a_non_executable_entrypoint_fails(archive: Path, tmp_path: Path) -> None:
    def strip_mode(info, data):
        new = zipfile.ZipInfo(info.filename, date_time=info.date_time)
        new.external_attr = (0o644 << 16) | 0o100000
        return new, data

    plain = repack(archive, tmp_path / "plain.zip", transform=strip_mode)
    report = check(plain, SMOKE_PARENT_ENV)

    assert any("not executable" in message for message in report.failures)


def test_a_missing_entrypoint_fails(archive: Path, tmp_path: Path) -> None:
    def drop_entrypoint(info, data):
        if info.filename == "run.sh":
            return None, data
        return info, data

    without = repack(archive, tmp_path / "without.zip", transform=drop_entrypoint)
    report = check(without, SMOKE_PARENT_ENV)

    assert any("missing" in message for message in report.failures)


@pytest.mark.parametrize(
    "smuggled",
    [".env", "agent/.venv/pyvenv.cfg", "agent/secrets.key", "agent/run.log", "agent/x.pyc"],
)
def test_excluded_content_is_caught_even_if_the_builder_let_it_through(
    archive: Path, tmp_path: Path, smuggled: str
) -> None:
    leaky = tmp_path / f"leaky-{smuggled.replace('/', '_')}.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(leaky, "w") as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        dst.writestr(smuggled, b"x")

    report = check(leaky, SMOKE_PARENT_ENV)
    assert any(smuggled in message for message in report.failures)


def test_path_traversal_is_caught(archive: Path, tmp_path: Path) -> None:
    hostile = tmp_path / "hostile.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(hostile, "w") as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        dst.writestr("../escaped.txt", b"x")

    report = check(hostile, SMOKE_PARENT_ENV)
    assert any("unsafe path" in message for message in report.failures)


def test_oversize_is_caught(archive: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(limits, "MAX_SUBMISSION_BYTES", 1)
    report = check(archive, SMOKE_PARENT_ENV)
    assert any("over the" in message for message in report.failures)


def test_a_mangled_instruction_is_caught_not_ignored(archive: Path, tmp_path: Path) -> None:
    import os

    # Break the entrypoint the way a quoting bug would: pass only the first word.
    def break_quoting(info, data):
        if info.filename == "run.sh":
            data = data.replace(b'"$@"', b"$1")
        return info, data

    broken = repack(archive, tmp_path / "quoting.zip", transform=break_quoting)
    report = check(broken, os.environ)

    assert any("did not arrive intact" in message for message in report.failures), (
        "exit 0 alone must not count as a working entrypoint contract"
    )


def test_crlf_in_the_entrypoint_fails(archive: Path, tmp_path: Path) -> None:
    # What a Windows checkout with core.autocrlf=true produces. On Linux the shebang
    # becomes `#!/bin/sh\r` and nothing starts.
    def to_crlf(info, data):
        if info.filename == "run.sh":
            data = data.replace(b"\n", b"\r\n")
        return info, data

    windows = repack(archive, tmp_path / "crlf.zip", transform=to_crlf)
    report = check(windows, SMOKE_PARENT_ENV)

    assert any("CRLF" in message for message in report.failures)


def test_a_missing_shebang_fails(archive: Path, tmp_path: Path) -> None:
    def strip_shebang(info, data):
        if info.filename == "run.sh":
            data = data.split(b"\n", 1)[1]
        return info, data

    headless = repack(archive, tmp_path / "headless.zip", transform=strip_shebang)
    report = check(headless, SMOKE_PARENT_ENV)

    assert any("shebang" in message for message in report.failures)
