"""Offline contract checks for the research-only assisted audit runner."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts.eval import run_suite as suite


@pytest.fixture
def diagnostic(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "docs/research/n3-source-diagnostic.py"
    spec = importlib.util.spec_from_file_location("n3_diagnostic", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The command wraps these entry points; restore them after each in-process test.
    monkeypatch.setattr(suite, "read_spec", suite.read_spec)
    monkeypatch.setattr(suite.Runner, "one", suite.Runner.one)
    monkeypatch.setattr(suite.Runner, "verify", suite.Runner.verify)
    monkeypatch.setattr(sys, "argv", [str(path)])
    return module


@pytest.mark.parametrize("copy_exit", [0, 1])
def test_official_tests_are_staged_and_capture_survives_success(
    diagnostic, monkeypatch, tmp_path, copy_exit
):
    """An absent /tests must not masquerade as a model's reward=0."""
    package = tmp_path / "package"
    (package / "tests").mkdir(parents=True)
    checker = b"# Unmodified official checker sentinel\n"
    (package / "tests/test_report.py").write_bytes(checker)
    container_tests = tmp_path / "container-tests"
    report = '{"findings": [{"title": "model output"}]}'
    verification_started = False

    def docker_boundary(cmd, **kwargs):
        nonlocal verification_started
        output, code = "", 0
        if cmd[:2] == ["docker", "cp"]:
            assert cmd[2:] == [f"{package / 'tests'}/.", "container:/tests"]
            code = copy_exit
            if code == 0:
                shutil.copytree(package / "tests", container_tests, dirs_exist_ok=True)
        elif "bash" in cmd:
            verification_started = True
        elif cmd[-2:] == ["cat", "/app/security_report.json"]:
            output = report
        elif cmd[-2:] == ["cat", suite.REWARD_PATH]:
            output = "1\n" if (container_tests / "test_report.py").exists() else "0\n"
        elif cmd[-2:] == ["cat", "/logs/verifier/pytest_output.txt"]:
            output = "3 passed\n"
        return subprocess.CompletedProcess(cmd, code, output, "copy failed" if code else "")

    def run_without_agent():
        runner = suite.Runner(SimpleNamespace(out=str(tmp_path / "out")), tmp_path)
        runner.n3_repeat = 1
        spec = SimpleNamespace(name="audit", root=package)
        reward, note = runner.verify("container", spec)
        if copy_exit:
            assert reward is None
            assert "stage" in note.lower()
            assert not verification_started
        else:
            assert reward == 1
            assert verification_started
            assert (container_tests / "test_report.py").read_bytes() == checker
            assert (runner.logs / "audit.r1.candidate").read_text() == report
            capture = json.loads((runner.logs / "audit.r1.capture.json").read_text())
            assert capture["reward"] == 1
            assert capture["report_sha256"] == hashlib.sha256(report.encode()).hexdigest()
            assert (runner.logs / "audit.r1.verifier").read_text() == "3 passed\n"
        return 0

    monkeypatch.setattr(suite, "run", docker_boundary)
    monkeypatch.setattr(suite, "main", run_without_agent)
    assert diagnostic.main() == 0


def test_only_environment_source_enters_assisted_instruction(diagnostic, monkeypatch):
    package = Path(__file__).resolve().parents[1] / "local_task/find-sqli-login"
    original = suite.read_spec(package)
    source = (package / "environment/app/routers/auth.py").read_text()

    def inspect_input():
        assisted = suite.read_spec(package)
        assert assisted.instruction.startswith(original.instruction + "\n\n")
        assert source in assisted.instruction
        assert "test_report_contains_login_sql_injection_finding" not in assisted.instruction
        assert "File: /app/routers/auth.py\n" in assisted.instruction
        return 0

    monkeypatch.setattr(sys, "argv", [sys.argv[0], "--source", "environment/app/routers/auth.py"])
    monkeypatch.setattr(suite, "main", inspect_input)
    assert diagnostic.main() == 0
