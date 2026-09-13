"""Reproduce four loop-mechanism findings from the 13.09.2026 review, offline.

Development tool. `agent/` never imports it and it never enters the archive. Runs in
about a second with the stub LLM from `tests/`, no endpoint and no Docker:

    python3 scripts/eval/probe_loop_guards.py

What it shows (observed on `fix/public-solves` `a9e1f4c`, 13.09.2026):

(a) The code-fix shell guard (`agent/loop.py::_code_fix_shell_refusal`) refuses
    ordinary, non-evaluator commands a small model writes by habit -- `pytest tests/
    -x -q 2>&1 | tail -n 30`, `cd /app && pytest tests/`, `python3 -c "import app"`,
    `python3 -m py_compile ...`, `curl ... /healthz`, `cat x | head`, `sed -i` with a
    regex metacharacter -- while accepting the plain `pytest tests/` and a literal
    `sed -i`. Each refusal costs a step and, because `|`/`&&`/`;` count as "may
    change state", advances the epoch, so the refused command is never blocked as a
    repeat (see b/c).
(b) Eight identical `write_file` calls of the same bytes execute eight times with no
    repeat block: `_advances_state_epoch` makes every write the sole record of its
    epoch, and `detectors.blocked`/`stuck` only read the current epoch. This is the
    shape of the E-BASE-r1 `find-sqli-login` zero (twenty identical empty writes).
(c) Eight identical *failing* `run_shell` commands that are not read-only execute
    eight times, for the same reason; the read-only control (`cat /missing`) is
    blocked from the third step as designed.
(d) `Budget` arithmetic at `total_s=600`: before the 90 s checkpoint the LLM call
    ceiling is `90 - elapsed` (89.5 s at t=0.5, 0.5 s at t=89.5), so a cold first
    call measured at 91 s is cut off; and from t=480 `may_act`, `may_recover` and
    every tool/verify timeout are zero although 120 s remain -- the verifier reserve
    is reachable by nothing, including the verifier.
(e) `tools.run_shell("sleep 8 &", timeout_s=3)` returns after 8 s with `ok=True`:
    the shell exits, the child keeps the stdout pipe, and the reader threads (and
    `close()`) wait for it. A model that starts a service with `&` freezes the agent
    until the runner's kill. (This step costs the probe ~8 s.)
(f) `sources.local_sources` raises `KeyError` when the entry point imports a module
    that does not parse; `preflight.inspect_environment` calls it unguarded, so the
    run dies with a traceback before the first LLM call.

None of this changes production code. Each item is a candidate factor in
`docs/plans/2026-09-13-gpu-queue.md`, to be fixed test-first and measured paired.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import loop, playbooks, sources, tools  # noqa: E402
from agent.state import Budget  # noqa: E402
from tests.stub_llm import StubLlm  # noqa: E402
from tests.test_loop import FakeVerify, action, make_state, run  # noqa: E402

GUARD_COMMANDS = (
    "pytest tests/",
    "pytest tests/ -q",
    "python3 -m pytest tests/ -q",
    "pytest tests/ -x -q 2>&1 | tail -n 30",
    "cd /app && pytest tests/",
    "pytest tests/ -q; echo EXIT=$?",
    "sed -i 's/name = .*/name = x/' app/routers/auth.py",
    "sed -i 's/fetchrow(query)/fetchrow(query, a, b)/' app/routers/auth.py",
    'python3 -c "import app"',
    "python3 -m py_compile app/routers/auth.py",
    "curl -s http://127.0.0.1:8000/healthz",
    "grep -rn fetchrow app/",
    "cat app/routers/auth.py | head -50",
    "head -50 app/routers/auth.py",
    "uvicorn main:app --port 8000 &",
    "git diff",
)


def workspace() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="probe-loop-guards-"))
    (tmp / "app" / "routers").mkdir(parents=True)
    (tmp / "app" / "routers" / "auth.py").write_text("x = 1\n", encoding="utf-8")
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_api.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    return tmp


def replay(tmp: Path, playbook: playbooks.Playbook, replies: list, max_steps: int) -> None:
    state = make_state(tmp)
    state.budget.max_steps = max_steps
    llm = StubLlm(replies=list(replies))
    outcome = run(state, llm, FakeVerify(), playbook=playbook)
    executed = sum(1 for event in state.audit if getattr(event, "kind", "") == "tool")
    blocks = [o for o in outcome.observations if "already" in o or "уже" in o]
    print(f"    stop={outcome.stop_reason} llm_calls={len(llm.calls)} "
          f"executed={executed} repeat_blocks={len(blocks)}")


def main() -> int:
    tmp = workspace()

    print("(a) code-fix shell guard")
    state = make_state(tmp)
    for command in GUARD_COMMANDS:
        verdict = "REFUSED" if loop._code_fix_shell_refusal(command, state) else "ok     "
        print(f"    {verdict}  {command}")

    audit = getattr(playbooks, "CODE_AUDIT", playbooks.GENERAL)
    target = tmp / "report.json"
    print("(b) 8 identical write_file of the same bytes, audit playbook")
    replay(tmp, audit, [action("write_file", path=str(target), content='{"findings": []}')] * 8, 10)

    print("(c) 8 identical failing non-read-only run_shell, forensics playbook")
    replay(tmp, playbooks.FORENSICS,
           [action("run_shell", command="python3 -c 'raise SystemExit(1)'")] * 8, 10)
    print("(c, control) 8 identical failing read-only run_shell")
    replay(tmp, playbooks.FORENSICS, [action("run_shell", command="cat /nonexistent/file")] * 8, 10)

    print("(d) Budget at total_s=600")
    now = [0.0]
    budget = Budget(total_s=600.0, clock=lambda: now[0])  # type: ignore[arg-type]
    for t in (0.5, 60.0, 85.0, 89.5, 95.0, 470.0, 479.0, 481.0):
        now[0] = t
        print(f"    t={t:5.1f}  may_act={budget.may_act()!s:5}  may_recover={budget.may_recover()!s:5}"
              f"  llm_timeout={budget.llm_timeout_s():5.1f}  tool_timeout={budget.tool_timeout_s():5.1f}"
              f"  remaining={budget.remaining_s():5.1f}")

    print("(e) run_shell with a background child, timeout_s=3 (takes ~8 s)")
    started = time.monotonic()
    result = tools.run_shell("sleep 8 &", cwd=tmp, timeout_s=3.0, secrets=())
    elapsed = time.monotonic() - started
    print(f"    returned after {elapsed:.1f}s ok={result.ok} exit={result.exit_code}"
          f"  -> {'timeout IGNORED' if elapsed > 6 else 'timeout honoured'}")

    print("(f) source index when the entry point imports a module with a SyntaxError")
    broken = tmp / "broken-ws"
    broken.mkdir()
    (broken / "main.py").write_text("import broken\nprint(broken.x)\n", encoding="utf-8")
    (broken / "broken.py").write_text('print "x"\n', encoding="utf-8")
    try:
        sources.local_sources(broken)
        print("    local_sources returned")
    except Exception as error:  # noqa: BLE001 - the point is to show what escapes
        print(f"    local_sources RAISED {type(error).__name__}: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
