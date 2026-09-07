"""Offline research probe, outside submission; never execute model-generated code.

Recompute the night tables, inspect saved requests, replay the official audit
checker on final saved reports, and reproduce the budget boundary with a fake clock.
Run from any directory; the saved build/ logs are required.
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent.state import Budget  # noqa: E402


def read_table(path):
    return list(csv.DictReader(path.read_text().splitlines(), delimiter="\t"))


def actions(text):
    parsed, invalid = [], 0
    for chunk in re.split(r"^--- call \d+ ---\n", text, flags=re.M)[1:]:
        try:
            value, _ = json.JSONDecoder().raw_decode(chunk[chunk.index("{"):])
            nested = value.get("arguments", {})
            value = {**(nested if isinstance(nested, dict) else {}), **value}
            value["tool"] = value.get("tool", value.get("name"))
            parsed.append(value)
        except (ValueError, TypeError, AttributeError):
            invalid += 1
    return parsed, invalid


def main():
    hashes = {}

    def source(relative):
        path = ROOT / relative
        hashes[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return path

    summary = read_table(source("build/deep-queue-summary.tsv"))
    suites, public = [], []
    checker_path = source("local_task/find-sqli-login/tests/test_security_report.py")
    spec = importlib.util.spec_from_file_location("official_audit_checker", checker_path)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    checker_tests = [getattr(checker, name) for name in sorted(dir(checker))
                     if name.startswith("test_")]
    for suite in summary:
        prefix = f"build/eval-{suite['label']}"
        rows = read_table(source(f"{prefix}/results.tsv"))
        solved = [r["task"] for r in rows if r["reward"] == "1"]
        tokens = sum(int(r["ep_tokens"]) for r in rows)
        wall = sum(float(r["wall_s"]) for r in rows)
        assert suite["solved"] == f"{len(solved)}/{len(rows)}"
        assert tokens == int(suite["tokens"])
        assert abs(wall - float(suite["wall_s"])) < 1.01
        suites.append(dict(label=suite["label"], set=suite["set"], solved=solved,
                           tasks=len(rows), endpoint_tokens=tokens, wall_s=round(wall, 1)))
        if suite["set"] != "public":
            continue
        for row in rows:
            base = f"{prefix}/logs/{row['task']}.r1"
            trace = source(base + ".trace").read_text()
            log = source(base + ".log").read_text()
            calls = json.loads(source(base + ".calls").read_text())
            requests = [json.loads(b["body"]) for b in calls["bodies"]]
            observations = {m["content"] for b in requests for m in b["messages"][1:]}
            acts, invalid = actions(trace)
            first = re.search(r"step=1 phase=plan status=end duration_s=([\d.]+)", log)
            reads = sorted({a.get("path", "?") for a in acts if a["tool"] == "read_range"})
            record = dict(label=suite["label"], task=row["task"], reward=int(row["reward"]),
                          first_plan_s=float(first[1]) if first else None,
                          saved_request_bodies=len(requests),
                          endpoint_error_visible_in_saved_prefix=any("endpoint error:" in m for m in observations),
                          emergency_write_hint_visible_in_saved_prefix=any("Запиши лучший вариант" in m for m in observations),
                          requested_read_paths=reads,
                          requested_write_paths=sorted({a.get("path", "?") for a in acts if a["tool"] == "write_file"}),
                          requested_shell_commands=list(dict.fromkeys(a.get("command", "?") for a in acts if a["tool"] == "run_shell")),
                          requested_finish=sum(a["tool"] == "finish" for a in acts),
                          invalid_trace_chunks=invalid)
            if row["task"] == "find-sqli-login":
                post = ROOT / (base + ".post")
                record["official_audit_replay"] = "unavailable: no saved final deliverable"
                if post.exists():
                    raw = source(base + ".post").read_text()
                    marker = "# claimed deliverable: /app/security_report.json\n"
                    if marker in raw:
                        content = raw.split(marker, 1)[1]
                        with tempfile.TemporaryDirectory(prefix="sber-public-replay-") as tmp:
                            checker.REPORT_PATH = Path(tmp) / "report.json"
                            checker.REPORT_PATH.write_text(content)
                            results = {}
                            for test in checker_tests:
                                try:
                                    test()
                                    results[test.__name__] = "PASS"
                                except (AssertionError, ValueError, TypeError) as exc:
                                    results[test.__name__] = f"FAIL: {type(exc).__name__}"
                            record["official_audit_replay"] = results
            public.append(record)

    for file in ("agent/state.py", "agent/limits.py", "agent/loop.py", "build/b-deep-base.zip"):
        source(file)
    clock = [1000.0]
    budget = Budget(total_s=600, clock=lambda: clock[0])
    budget.start_step()
    initial = budget.llm_timeout_s()
    clock[0] += 85.731
    budget.start_step()
    second = budget.llm_timeout_s()
    clock[0] += 4.4
    boundary = dict(initial_llm_timeout_s=initial, second_llm_timeout_s=round(second, 3),
                    past_early_deadline=budget.past_early_deadline(),
                    remaining_s=round(budget.remaining_s(), 3),
                    spendable_s=round(budget.spendable_s(), 3), steps_used=budget.steps_used)
    assert 4 < second < 5 and boundary["past_early_deadline"]
    assert boundary["spendable_s"] > 380
    print(json.dumps(dict(scope="offline analysis, no new model calls or end-to-end rewards",
                          caveat="Trace actions are requests, not execution evidence. Only first three request bodies saved.",
                          suites=suites, public=public, budget_boundary=boundary,
                          source_sha256=hashes), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
