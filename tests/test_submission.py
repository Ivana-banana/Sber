"""Packaging is the one defect class that zeroes all fifteen tasks at once.

Everything here works on a built archive, never on the source tree, because that is
the difference the check is for.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from agent import limits
from scripts import check_submission
from scripts.build_submission import ROOT, build
from scripts.check_submission import CONTRACT_LABEL, apply_declared_modes, check

SMOKE_PARENT_ENV: dict[str, str] = {}
"""Deliberately empty in most tests: the cold start is exercised separately, and an
empty parent makes it report SKIP or FAIL rather than depending on the developer's
machine."""

SHIPPED_PUBLIC_FIXTURE_IDENTIFIERS = (
    b"bye-file",
    b"hello-file",
    b"find-sqli-login",
    b"fix-sqli-login",
    b"fix-sqli-search",
    b"incident-log-forensics",
    b"bye.txt",
    b"hello.txt",
    b"security_report.json",
    b"incident_report.txt",
    b'"bye"',
)


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


def test_archive_contains_no_public_fixture_identifiers(archive: Path) -> None:
    """Public examples may shape tests, but their identifiers must never ship."""
    offenders: dict[str, list[str]] = {}
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            data = zf.read(name).lower()
            for identifier in SHIPPED_PUBLIC_FIXTURE_IDENTIFIERS:
                if identifier in data:
                    offenders.setdefault(identifier.decode("ascii"), []).append(name)

    assert offenders == {}, f"public fixture identifiers shipped: {offenders}"


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


def clear_utf8_flag(data: bytes, name: str) -> bytes:
    """Turn off the UTF-8 name flag (bit 0x800) for one entry, in the raw zip bytes.

    `zipfile`'s writer always sets this flag itself the moment a name is not pure
    ASCII (`ZipInfo._encodeFilenameFlags`), so there is no way to ask it to write a
    non-compliant archive -- the very thing this check exists for. The only way to
    produce one is to patch the bytes after writing, at the fixed offsets APPNOTE
    defines for the local file header (flag at +6) and the central directory file
    header (flag at +8), located by walking back from the encoded name.
    """
    patched = bytearray(data)
    name_bytes = name.encode("utf-8")
    start = 0
    while True:
        idx = patched.find(name_bytes, start)
        if idx == -1:
            break
        start = idx + 1
        local = idx - 30
        if local >= 0 and bytes(patched[local : local + 4]) == b"PK\x03\x04":
            flag_off = local + 6
            flags = struct.unpack_from("<H", patched, flag_off)[0]
            struct.pack_into("<H", patched, flag_off, flags & ~0x800)
        central = idx - 46
        if central >= 0 and bytes(patched[central : central + 4]) == b"PK\x01\x02":
            flag_off = central + 8
            flags = struct.unpack_from("<H", patched, flag_off)[0]
            struct.pack_into("<H", patched, flag_off, flags & ~0x800)
    return bytes(patched)


def inflate_declared_size(data: bytes, name: str, size: int) -> bytes:
    """Set one entry's declared uncompressed size, in the raw zip bytes, to `size`.

    `ZipFile.writestr()` unconditionally sets `zinfo.file_size = len(data)` right
    before writing, clobbering any size set on the `ZipInfo` beforehand -- so there is
    no way to ask it to write a member that claims a size its compressed data does not
    have. The only way to produce one is to patch the bytes after writing, at the
    fixed offsets APPNOTE defines for the local file header (uncompressed size at +22)
    and the central directory file header (uncompressed size at +24), the same
    approach `clear_utf8_flag` uses for the flag field.
    """
    patched = bytearray(data)
    name_bytes = name.encode("utf-8")
    start = 0
    while True:
        idx = patched.find(name_bytes, start)
        if idx == -1:
            break
        start = idx + 1
        local = idx - 30
        if local >= 0 and bytes(patched[local : local + 4]) == b"PK\x03\x04":
            struct.pack_into("<I", patched, local + 22, size)
        central = idx - 46
        if central >= 0 and bytes(patched[central : central + 4]) == b"PK\x01\x02":
            struct.pack_into("<I", patched, central + 24, size)
    return bytes(patched)


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


# --- §2 of verdict-packaging-pitfalls.md: what the checker used to miss --------


def test_a_backslash_in_a_name_is_unsafe() -> None:
    # Not an end-to-end archive test like its neighbours: on a machine where
    # `os.sep == "\\"`, `zipfile`'s own reader normalises every backslash to `/`
    # while parsing the central directory (`ZipInfo.__init__`, invoked from
    # `_RealGetContents`) before `check_archive` ever sees a name -- there is no way
    # to build a real archive that round-trips a backslash through `zipfile.ZipFile`
    # on this platform. On Linux, where `os.sep == "/"`, nothing normalises it, and
    # `Path(name).parts` (the old, platform-dependent parser) would have missed it.
    # The helper itself has to reject a backslash regardless of which platform
    # happens to be running the check.
    assert check_submission._unsafe_path_reason("agent\\evil.py") is not None


def test_a_clean_posix_name_is_safe() -> None:
    assert check_submission._unsafe_path_reason("agent/main.py") is None


def test_a_drive_letter_prefixed_name_is_unsafe_not_a_crash(
    archive: Path, tmp_path: Path
) -> None:
    # The defect this used to be: `apply_declared_modes` does
    # `(dest / info.filename).chmod(mode)` with no sanitising. `dest / "C:/evil.txt"`
    # collapses to an absolute path on Windows, so an untrusted archive controlled a
    # filesystem operation outside the extraction directory. It must never reach that
    # code at all -- `check_archive` has to reject the name first.
    hostile = tmp_path / "drive.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(hostile, "w") as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        # A Unix host byte with a real mode, the way an archive actually built on
        # Linux would declare it -- `writestr(name, bytes)` on this (Windows) machine
        # would default to a DOS host byte and never reach `apply_declared_modes` at
        # all, silently failing to exercise the vulnerable path.
        evil = zipfile.ZipInfo("C:/evil.txt")
        evil.create_system = 3
        evil.external_attr = 0o644 << 16
        dst.writestr(evil, b"x")

    report = check(hostile, SMOKE_PARENT_ENV)
    assert any("evil.txt" in message for message in report.failures)
    assert not any("evil.txt" in message for message in report.skipped)


def test_a_symlink_member_is_rejected(archive: Path, tmp_path: Path) -> None:
    hostile = tmp_path / "symlink.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(hostile, "w") as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        link = zipfile.ZipInfo("agent/escape")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        dst.writestr(link, b"/etc/passwd")

    report = check(hostile, SMOKE_PARENT_ENV)
    assert any("agent/escape" in message for message in report.failures)


def test_an_oversized_member_is_caught_even_in_a_small_archive(
    archive: Path, tmp_path: Path, monkeypatch
) -> None:
    # A member can declare a huge uncompressed size while compressing to almost
    # nothing (e.g. a run of zeros), so the whole-archive size check alone misses it.
    # Patched directly into the raw bytes (see inflate_declared_size) -- setting
    # file_size on a ZipInfo before writestr() is silently overwritten.
    monkeypatch.setattr(limits, "MAX_SUBMISSION_BYTES", 1024)

    bomb = tmp_path / "bomb.zip"
    bomb.write_bytes(inflate_declared_size(archive.read_bytes(), "run.sh", 11 * 1024 * 1024))
    report = check(bomb, SMOKE_PARENT_ENV)

    assert any("uncompressed" in message for message in report.failures)


def test_a_non_ascii_name_without_the_utf8_flag_is_caught(
    archive: Path, tmp_path: Path
) -> None:
    name = "agent/nаme.py"  # Cyrillic а
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(
        tmp_path / "efs-source.zip", "w"
    ) as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        dst.writestr(name, b"x")

    hostile = tmp_path / "efs.zip"
    hostile.write_bytes(clear_utf8_flag((tmp_path / "efs-source.zip").read_bytes(), name))

    report = check(hostile, SMOKE_PARENT_ENV)
    assert any("UTF-8" in message for message in report.failures)


def test_a_non_ascii_name_with_the_utf8_flag_is_accepted(
    archive: Path, tmp_path: Path
) -> None:
    clean = tmp_path / "efs-ok.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(clean, "w") as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        info = zipfile.ZipInfo("agent/nаme.py")
        info.flag_bits |= 0x800
        dst.writestr(info, b"x")

    report = check(clean, SMOKE_PARENT_ENV)
    assert not any("UTF-8" in message for message in report.failures)


def test_a_lone_cr_in_the_entrypoint_fails(archive: Path, tmp_path: Path) -> None:
    # Old Mac Classic line endings: a bare CR with no following LF. `CRLF in data`
    # does not see it, so it used to slip through as if the file had Unix endings.
    def to_lone_cr(info, data):
        if info.filename == "run.sh":
            data = data.replace(b"\n", b"\r")
        return info, data

    classic = repack(archive, tmp_path / "classic-mac.zip", transform=to_lone_cr)
    report = check(classic, SMOKE_PARENT_ENV)

    assert any("CR" in message for message in report.failures)


def test_a_bom_before_the_shebang_is_named_not_reported_as_missing(
    archive: Path, tmp_path: Path
) -> None:
    def add_bom(info, data):
        if info.filename == "run.sh":
            data = b"\xef\xbb\xbf" + data
        return info, data

    bommed = repack(archive, tmp_path / "bom.zip", transform=add_bom)
    report = check(bommed, SMOKE_PARENT_ENV)

    assert any("BOM" in message for message in report.failures)
    assert not any("no shebang" in message for message in report.failures)
