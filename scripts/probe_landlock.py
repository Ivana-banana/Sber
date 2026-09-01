"""Ask the running kernel whether Landlock self-restriction is available here.

`docs/research/shell-confinement.md` puts Landlock first among the mechanisms a
container can apply to *itself*: no package, no `CAP_SYS_ADMIN`, allow-list by path.
Every other candidate in that table either needs a binary we cannot install
(`bwrap`), a capability we do not have (AppArmor, blocking `fanotify`), or is not a
boundary at all (`chdir`, deny-lists). So whether `run_shell` can be confined at all
comes down to one question this script answers.

Read-only. It creates no ruleset and restricts nothing: `landlock_create_ruleset`
with a NULL attribute and the VERSION flag is the documented way to ask for the ABI
level and nothing else, and `PR_GET_NO_NEW_PRIVS` only reads the current bit.

Not part of the submission: `scripts/build_submission.py` ships `run.sh`,
`THIRD_PARTY_NOTICES.md` and `agent/` only.

**A result here does not transfer to the evaluation host.** A container has no kernel
of its own -- it uses the host's. Run inside `secureintelligent/acp` on this laptop
the answer describes the WSL2 kernel; run on the organizers' microVM it would
describe theirs. Only a negative result about the *syscall interface* (ENOSYS on
every kernel) would be portable, and that is not what this measures.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import platform
import sys

# x86_64 syscall numbers; Landlock landed as 444/445/446 in Linux 5.13.
SYS_LANDLOCK_CREATE_RULESET = 444
PR_SET_NO_NEW_PRIVS = 38
PR_GET_NO_NEW_PRIVS = 39
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

ABI_FEATURES = {
    1: "RW allow-list on files and directories",
    2: "+ refer (link/rename across directories)",
    3: "+ truncate",
    4: "+ TCP bind/connect scoping",
    5: "+ ioctl on devices",
}


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def landlock_abi(libc: ctypes.CDLL) -> tuple[int | None, str]:
    """Return (abi_version, reason). `None` means the kernel gave no version."""
    ctypes.set_errno(0)
    result = libc.syscall(
        ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET),
        ctypes.c_void_p(None),
        ctypes.c_size_t(0),
        ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION),
    )
    if result >= 0:
        return result, ""
    code = ctypes.get_errno()
    if code == errno.ENOSYS:
        return None, "ENOSYS: kernel has no landlock_create_ruleset syscall"
    if code == errno.EOPNOTSUPP:
        return None, "EOPNOTSUPP: syscall exists but Landlock is disabled in this kernel"
    if code == errno.EPERM:
        return None, "EPERM: blocked before the kernel -- a seccomp profile, most likely"
    return None, f"errno {code}: {os.strerror(code)}"


def no_new_privs(libc: ctypes.CDLL) -> tuple[bool, str]:
    """Can we set `no_new_privs` without a capability? Landlock requires it."""
    ctypes.set_errno(0)
    already = libc.prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0)
    if already == 1:
        return True, "already set by the runtime"
    ctypes.set_errno(0)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0:
        return True, "set by this process"
    code = ctypes.get_errno()
    return False, f"errno {code}: {os.strerror(code)}"


def main() -> int:
    print(f"kernel:  {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"python:  {sys.version.split()[0]}")

    if platform.system() != "Linux":
        # `os.geteuid` does not exist on Windows, so this check comes before it.
        print("verdict: NOT APPLICABLE -- Landlock is a Linux interface")
        return 2
    print(f"euid:    {os.geteuid()}")
    if platform.machine() != "x86_64":
        print("verdict: NOT RUN -- the syscall numbers here are x86_64 only")
        return 2

    libc = _libc()
    abi, reason = landlock_abi(libc)
    nnp, nnp_reason = no_new_privs(libc)

    print(f"no_new_privs: {'yes' if nnp else 'no'} ({nnp_reason})")
    if abi is None:
        print(f"landlock ABI: unavailable ({reason})")
        print("verdict: NO -- confine run_shell from outside, or not at all")
        return 1

    print(f"landlock ABI: {abi} ({ABI_FEATURES.get(abi, 'newer than this script knows')})")
    if not nnp:
        print("verdict: NO -- ABI is present but no_new_privs is refused, and "
              "landlock_restrict_self needs it")
        return 1
    print("verdict: YES -- a path allow-list is enforceable from inside this process")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
