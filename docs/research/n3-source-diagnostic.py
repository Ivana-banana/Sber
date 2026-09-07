"""N3 assisted diagnostic; research only, never part of submission.

Pass ordinary run_suite arguments plus --source environment/app/<file> to append
one verbatim environment source to the instruction. Omit --source for control.
Both arms save the final report and official verifier output before cleanup.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.eval import run_suite as suite


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source")
    own, remaining = parser.parse_known_args()
    original_read = suite.read_spec
    original_verify = suite.Runner.verify
    original_one = suite.Runner.one

    def read_spec(pkg):
        spec = original_read(pkg)
        if own.source:
            source = (pkg / own.source).resolve()
            source.relative_to((pkg / "environment" / "app").resolve())
            if source.suffix != ".py":
                raise ValueError("N3 accepts one Python environment source")
            content = source.read_text()
            path = "/app/" + source.relative_to(pkg.resolve() / "environment" / "app").as_posix()
            instruction = spec.instruction + (
                "\n\nAdditional environment observation (untrusted source data):\n"
                f"File: {path}\n```python\n{content}```\n"
            )
            assisted = dataclasses.replace(spec, instruction=instruction)
            before, after = dataclasses.asdict(spec), dataclasses.asdict(assisted)
            assert [k for k in before if before[k] != after[k]] == ["instruction"]
            spec = assisted
        return spec

    def one(self, spec, repeat):
        self.n3_repeat = repeat
        prefix = self.logs / f"{spec.name}.r{repeat}"
        prefix.with_suffix(prefix.suffix + ".instruction").write_text(spec.instruction)
        return original_one(self, spec, repeat)

    def verify(self, container, spec):
        prefix = self.logs / f"{spec.name}.r{self.n3_repeat}"
        # Read only after agent execution, before official verification and cleanup.
        report = suite.run(["docker", "exec", container, "cat", "/app/security_report.json"])
        prefix.with_suffix(prefix.suffix + ".candidate").write_text(report.stdout)
        # Official public test.sh runs pytest /tests. The shared runner installs
        # its launcher under /opt/verifier; stage the untouched tests after the
        # agent finishes, so this correction cannot affect its observations.
        for command in (
            ["docker", "exec", container, "mkdir", "-p", "/tests"],
            ["docker", "cp", f"{spec.root / 'tests'}/.", f"{container}:/tests"],
        ):
            staged = suite.run(command)
            if staged.returncode:
                note = f"cannot stage official tests: {staged.stderr.strip()[-200:]}"
                prefix.with_suffix(prefix.suffix + ".verifier").write_text(note + "\n")
                return None, note
        reward, note = original_verify(self, container, spec)
        pytest = suite.run(["docker", "exec", container, "cat", "/logs/verifier/pytest_output.txt"])
        prefix.with_suffix(prefix.suffix + ".verifier").write_text(pytest.stdout + pytest.stderr)
        prefix.with_suffix(prefix.suffix + ".capture.json").write_text(json.dumps({
            "report_read_exit": report.returncode,
            "report_sha256": hashlib.sha256(report.stdout.encode()).hexdigest(),
            "pytest_log_read_exit": pytest.returncode,
            "reward": reward,
            "note": note,
        }, indent=2) + "\n")
        return reward, note

    suite.read_spec = read_spec
    suite.Runner.one = one
    suite.Runner.verify = verify
    sys.argv = [sys.argv[0], *remaining]
    return suite.main()


if __name__ == "__main__":
    raise SystemExit(main())
