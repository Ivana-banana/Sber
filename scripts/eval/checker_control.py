"""Positive/negative control for every public checker.

Negative: untouched environment must score 0. Positive: the package's own solution
must score 1. A checker that cannot tell those apart cannot score an agent either.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path("scripts/eval").resolve()))
from run_suite import read_spec  # noqa: E402

NETWORK = "sber-eval"


def sh(cmd, timeout=300):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def start(spec):
    cmd = ["docker", "run", "-d", "--rm", "--network", NETWORK,
           f"--cpus={spec.cpus}", f"--memory={spec.memory_mb}m",
           f"sber-eval/{spec.name}:latest"]
    if not spec.has_entrypoint:
        cmd += ["tail", "-f", "/dev/null"]
    out = sh(cmd)
    if out.returncode != 0:
        raise SystemExit(f"start {spec.name}: {out.stderr.strip()[-300:]}")
    return out.stdout.strip()


def reward(container, spec):
    sh(["docker", "exec", container, "mkdir", "-p", "/tests", "/logs/verifier"])
    sh(["docker", "cp", f"{spec.root / 'tests'}/.", f"{container}:/tests"])
    checked = sh(["docker", "exec", container, "bash", "/tests/test.sh"], timeout=300)
    got = sh(["docker", "exec", container, "cat", "/logs/verifier/reward.txt"])
    if got.returncode != 0:
        return None, f"no reward file; test.sh rc={checked.returncode} {checked.stderr.strip()[-200:]}"
    return got.stdout.strip(), ""


def solve(container, spec):
    sh(["docker", "exec", container, "mkdir", "-p", "/solution"])
    sh(["docker", "cp", f"{spec.root / 'solution'}/.", f"{container}:/solution"])
    sh(["docker", "exec", container, "chmod", "+x", "/solution/solve.sh"])
    return sh(["docker", "exec", container, "bash", "/solution/solve.sh"], timeout=300)


def main():
    tasks = sorted(p for p in Path("local_task").iterdir() if (p / "task.toml").exists())
    failures = 0
    for pkg in tasks:
        spec = read_spec(pkg)
        for arm in ("negative", "positive"):
            container = start(spec)
            try:
                note = ""
                if arm == "positive":
                    done = solve(container, spec)
                    if done.returncode != 0:
                        note = f" solve.sh rc={done.returncode} {done.stderr.strip()[-160:]}"
                got, why = reward(container, spec)
                want = "0" if arm == "negative" else "1"
                ok = got == want
                failures += 0 if ok else 1
                print(f"{spec.name:26s} {arm:8s} reward={got!s:6s} want={want} "
                      f"{'OK' if ok else 'MISMATCH'}{why}{note}", flush=True)
            finally:
                sh(["docker", "rm", "-f", container])
    print(f"\nmismatches: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
