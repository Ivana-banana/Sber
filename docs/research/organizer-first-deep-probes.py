"""Read-only runtime diagnostics; temporary workspaces, optional offline containers.

Run from the repository: python3 docs/research/organizer-first-deep-probes.py
Add --containers to replay saved E060 checks in locally cached task images.
No LLM, image builds, dependency installation or source edits are performed.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent import loop, playbooks, tools  # noqa: E402
from agent.ports import LlmReply  # noqa: E402
from agent.preflight import EnvironmentBrief  # noqa: E402
from agent.state import AgentState, Budget  # noqa: E402
from agent.verify import verify  # noqa: E402
from tests.stub_llm import StubLlm  # noqa: E402


def actions(path: Path) -> list[dict]:
    result = []
    for chunk in re.split(r"^--- call \d+ ---\n", path.read_text(), flags=re.M)[1:]:
        text = chunk.strip()
        if text.startswith("```"):
            text = "\n".join(text.splitlines()[1:-1])
        try:
            action = json.loads(text)
        except ValueError:
            continue
        if "name" in action and "arguments" in action:
            action = {"tool": action["name"], **action["arguments"]}
        result.append(action)
    return result


def reply(tool: str, **fields: object) -> LlmReply:
    return LlmReply(text=json.dumps({"tool": tool, **fields}))


def run_scripted(root: Path, replies: list[LlmReply]):
    state = AgentState(
        instruction=f"Write the final result to {root / 'answer.txt'}",
        workspace=root,
        budget=Budget(600),
    )
    model = StubLlm(replies)
    outcome = loop.run(
        state, model, verify,
        brief=EnvironmentBrief(root, True), playbook=playbooks.GENERAL,
    )
    return state, outcome


def local_probes() -> dict:
    result = {}
    with tempfile.TemporaryDirectory(prefix="sber-deep-probe-") as directory:
        root = Path(directory)
        source = root / "source.txt"
        source.write_text("first\nsecond\n")
        pattern = r"first\nsecond"
        result["search_directory_finds_line"] = tools.search("first", root).ok
        result["search_existing_file_finds_line"] = tools.search("first", source).ok
        result["full_text_regex_matches"] = bool(re.search(pattern, source.read_text()))
        result["line_search_multiline_matches"] = tools.search(pattern, root).ok

        target = root / "answer.txt"
        state, out = run_scripted(root, [
            reply("write_file", path=str(target), content=f"version {i}")
            for i in range(1, 7)
        ] + [reply("finish", path=str(target), verify="test -s answer.txt")])
        result["different_equal_length_writes"] = {
            "stop": out.stop_reason,
            "steps": state.budget.steps_used,
            "content": target.read_text(),
            "novel": [action.novel for action in state.actions],
        }
        target.unlink()

        state, out = run_scripted(root, [
            reply("write_file", path=str(root / "inspect.py"), content="print(1)"),
            *[reply("read_range", path=str(source), start_line=1)] * 4,
            reply("write_file", path=str(target), content="answer"),
        ])
        result["scratch_counts_as_result"] = {
            "stop": out.stop_reason,
            "deliverable": out.deliverable.name if out.deliverable else None,
            "required_output_exists": target.exists(),
        }

        state, out = run_scripted(root, [
            reply("run_shell", command="printf OK > answer.txt"),
            *[reply("read_range", path=str(source), start_line=1)] * 4,
        ])
        result["shell_write_tracking"] = {
            "exists": target.exists(), "tracked": len(state.artifacts),
            "deliverable": str(out.deliverable) if out.deliverable else None,
        }

        state, out = run_scripted(root, [
            reply("write_file", path=str(target), content="OK"),
            reply("finish", arguments={"path": str(target), "verify": "test -s answer.txt"}),
        ])
        result["nested_finish_refused"] = any(
            "finish refused: no `path`" in item for item in out.observations
        )
    return result


def saved_trace_counts() -> dict:
    result = {}
    for experiment in ("E049-public-repeat", "E060-artifact-guard", "E061-recon-packet"):
        files = sorted((ROOT / f"build/eval-{experiment}/logs").glob("*.trace"))
        if not files:
            result[experiment] = {"status": "saved traces unavailable"}
            continue
        counts = Counter(a.get("tool") for path in files for a in actions(path))
        result[experiment] = {"trace_files": len(files), "requested_actions": dict(counts)}
    return result


def saved_report_replays() -> dict:
    """Replay pure file-report checkers; redirect only the candidate path in memory."""
    result = {}
    with tempfile.TemporaryDirectory(prefix="sber-report-probe-") as directory:
        target = Path(directory) / "candidate"
        for experiment in ("E060-artifact-guard", "E061-recon-packet"):
            tasks = {}
            for path in sorted((ROOT / f"build/eval-{experiment}/logs").glob("*.trace")):
                task = path.name.removesuffix(".r1.trace")
                if not any(kind in task for kind in ("audit", "forensics")):
                    continue
                checker = ROOT / f"local_task_extra/tasks/{task}/tests/check.py"
                if not checker.exists():
                    continue
                versions = []
                for action in actions(path):
                    if action.get("tool") != "write_file":
                        continue
                    source = checker.read_text().replace(action["path"], str(target))
                    if str(target) not in source:
                        continue
                    target.write_text(action["content"])
                    try:
                        exec(compile(source, str(checker), "exec"), {})
                    except SystemExit as error:
                        passed = error.code in (0, None)
                    else:
                        passed = True
                    versions.append(int(passed))
                tasks[task] = versions
            result[experiment] = tasks
    return result


def offline_finish_replays() -> dict:
    result = {}
    container_code = """import json, sys, subprocess
from pathlib import Path
packet = json.load(sys.stdin)
for action in packet['writes']:
    path = Path(action['path'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(action['content'])
checked = subprocess.run(packet['command'], shell=True, executable='/bin/sh',
                         cwd=packet['cwd'], capture_output=True, text=True, timeout=5)
print(json.dumps({'exit_code': checked.returncode,
                  'output': (checked.stderr or checked.stdout)[-900:]}))
"""
    for task in (
        "audit-path-normalization", "fix-zip-slip-extract", "mut-audit-query-gate",
        "mut-fix-inventory-filter", "mut-fix-session-lookup",
    ):
        path = ROOT / f"build/eval-E060-artifact-guard/logs/{task}.r1.trace"
        if not path.exists():
            result[task] = {"status": "saved trace unavailable"}
            continue
        writes = []
        finish = None
        for action in actions(path):
            if action.get("tool") == "write_file":
                writes.append(action)
            if action.get("tool") == "finish":
                finish = action
                break
        if finish is None:
            result[task] = {"status": "no recorded finish"}
            continue
        packet = {
            "writes": writes, "command": finish["verify"],
            "cwd": "/workspace/review" if task == "mut-audit-query-gate" else "/app",
        }
        run = subprocess.run([
            "docker", "run", "--rm", "--pull=never", "--network", "none",
            "--entrypoint", "python3", "-i", f"sber-eval/{task}:latest",
            "-c", container_code,
        ], input=json.dumps(packet), capture_output=True, text=True, timeout=20)
        if run.returncode:
            raise RuntimeError(f"container replay {task}: {run.stderr[-500:]}")
        result[task] = json.loads(run.stdout)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--containers", action="store_true")
    arguments = parser.parse_args()
    output = {
        "local_probes": local_probes(), "saved_traces": saved_trace_counts(),
        "saved_report_replays": saved_report_replays(),
    }
    if arguments.containers:
        output["offline_finish_replays"] = offline_finish_replays()
    print(json.dumps(output, ensure_ascii=False, indent=2))
