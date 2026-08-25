"""Packaging is the one defect class that zeroes all fifteen tasks at once.

Everything here works on a built archive, never on the source tree, because that is
the difference the check is for.
"""

from __future__ import annotations

import hashlib
import os
import stat
import zipfile
from pathlib import Path

import pytest

from agent import limits
from scripts.build_submission import ROOT, build
from scripts.check_submission import CONTRACT_LABEL, apply_declared_modes, check

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
        assert top in {"run.sh", "agent", "THIRD_PARTY_NOTICES.md"}, f"unexpected entry: {name}"


def test_caches_and_compiled_files_never_ship(archive: Path) -> None:
    names = zipfile.ZipFile(archive).namelist()
    assert not [n for n in names if "__pycache__" in n or n.endswith((".pyc", ".pyo"))]


def test_entrypoint_is_executable_inside_the_archive(archive: Path) -> None:
    # Windows has no executable bit on disk, so this can only come from the builder
    # setting it. Getting it wrong means the runner cannot start us at all.
    mode = zipfile.ZipFile(archive).getinfo("run.sh").external_attr >> 16
    assert mode & 0o111, f"run.sh mode is {mode:o}"


def test_the_archive_says_it_came_from_unix_or_that_mode_is_ignored(archive: Path) -> None:
    # The mode above lives in the high half of external_attr, and an extractor reads
    # it only when the central directory says the archive was made on Unix. `ZipInfo`
    # takes that byte from the build platform, so a Windows build ships a correct
    # 0o755 next to a "made on DOS" marker and `unzip` drops the bit. Then `./run.sh`
    # is Permission denied before the first LLM call, and the test above still passes
    # because it reads back the number we wrote ourselves.
    info = zipfile.ZipFile(archive).getinfo("run.sh")
    assert info.create_system == 3, f"run.sh declares host {info.create_system}, not 3 (Unix)"

    # And the mode has to carry its file type, because that is the same field an
    # extractor reads to tell a regular file from a symlink. Type bits left at zero
    # make the entry a file of no type, which is not what any of them expect.
    mode = info.external_attr >> 16
    assert stat.S_ISREG(mode), f"run.sh mode 0o{mode:o} has no regular-file type bits"


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


def to_dos_host(info, data):
    """Rewrite an entry as a Windows build that forgot to declare a Unix host.

    The mode stays a perfectly correct 0o755. Only the byte saying how to read it
    changes, which is precisely why this defect survives an inspection of the mode.
    """
    new = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    new.create_system = 0
    new.external_attr = info.external_attr
    new.compress_type = info.compress_type
    return new, data


def test_a_good_archive_has_no_blocking_problems(archive: Path) -> None:
    import os

    report = check(archive, os.environ)
    assert report.failures == []
    assert any("run.sh is present at the root" in message for message in report.passed)


def contract_messages(report) -> tuple[list[str], list[str], list[str]]:
    """The three places the contract run can report itself, in one call."""
    pick = lambda bucket: [m for m in bucket if m.startswith(CONTRACT_LABEL)]  # noqa: E731
    return pick(report.passed), pick(report.failures), pick(report.skipped)


def test_the_cold_start_contract_reports_exactly_one_result(archive: Path) -> None:
    import os

    passed, failed, skipped = contract_messages(check(archive, os.environ))
    outcomes = len(passed) + len(failed) + len(skipped)

    assert outcomes == 1, (
        "the contract run must say what happened exactly once; "
        f"got {outcomes} (pass={passed} fail={failed} skip={skipped})"
    )


def test_extraction_applies_the_mode_the_archive_declares(archive: Path, tmp_path: Path) -> None:
    # `extractall` restores no permissions on any platform, so an extracted tree proves
    # nothing on its own. The checker has to reproduce what a mode-restoring extractor
    # does -- read the mode out of the central directory -- rather than chmod a constant
    # it chose itself, which would pass whatever the builder wrote.
    with zipfile.ZipFile(archive) as zf:
        dest = tmp_path / "unpacked"
        zf.extractall(dest)
        applied = apply_declared_modes(zf, dest)

    assert applied.get("run.sh") == 0o755, f"declared modes applied: {applied}"


def test_a_dos_host_byte_leaves_the_entrypoint_without_a_mode(
    archive: Path, tmp_path: Path
) -> None:
    dos = repack(archive, tmp_path / "dos.zip", transform=to_dos_host)

    with zipfile.ZipFile(dos) as zf:
        dest = tmp_path / "unpacked"
        zf.extractall(dest)
        applied = apply_declared_modes(zf, dest)

    assert "run.sh" not in applied, (
        "a DOS host byte means the 0o755 next to it is not a Unix mode, "
        "and an extractor is entitled to ignore it"
    )


def test_a_dos_host_archive_never_gets_a_contract_pass(archive: Path, tmp_path: Path) -> None:
    # The regression this whole split exists for. The old cold start chmod'ed the
    # entrypoint and launched it as `sh run.sh`, so it repaired the exact defect it was
    # supposed to catch and reported PASS on an archive the runner could not start.
    dos = repack(archive, tmp_path / "dos.zip", transform=to_dos_host)
    passed, failed, skipped = contract_messages(check(dos, SMOKE_PARENT_ENV))

    assert not passed, f"an unusable archive was reported as passing: {passed}"
    assert failed or skipped, "the contract run has to report the DOS archive, not stay silent"


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot exec a shell script directly")
def test_a_dos_host_archive_fails_the_contract_on_posix(archive: Path, tmp_path: Path) -> None:
    dos = repack(archive, tmp_path / "dos.zip", transform=to_dos_host)
    _, failed, _ = contract_messages(check(dos, os.environ))

    assert failed, "on a platform that can run ./run.sh, a lost mode is a failure, not a skip"


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
