"""Read-only diagnosis of archived E048 answers; never calls a model or service.

Run from the repository root: python3 docs/research/live-runs/E048-replay.py
The report verifier is imported unchanged, with only its output path redirected
to a temporary directory. Fix cases use AST equality, not a service-test replay.
"""

import ast
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tempfile

ROOT = Path(__file__).resolve().parents[3]
LOGS = ROOT / "build/eval-E048-public-gpu/logs"


def actions(name):
    text = (LOGS / f"{name}.r1.trace").read_text()
    for number, body in re.findall(
        r"--- call (\d+) ---\n(.*?)(?=--- call |\Z)", text, re.S
    ):
        value = json.JSONDecoder().raw_decode(body[body.index("{"):])[0]
        if "arguments" in value:
            value = {"tool": value.get("name"), **value["arguments"]}
        yield int(number), value


def main():
    rows = list(csv.DictReader((LOGS.parent / "results.tsv").open(), delimiter="\t"))
    for row in rows:
        if row["reward"] != "0":
            continue
        name = row["task"]
        aa = list(actions(name))
        writes = [(n, a) for n, a in aa if a.get("tool") == "write_file"]
        print(name, "calls", len(aa), "write_calls", [n for n, _ in writes],
              "wall/600", round(float(row["wall_s"]) / 600, 4), "stop", row["stop"])
        print("  reads", sorted({a.get("path", "<missing>") for _, a in aa
                                 if a.get("tool") == "read_range"}))
        print("  finish_calls", sum(a.get("tool") == "finish" for _, a in aa))
        print("  trace_sha256", hashlib.sha256(
            (LOGS / f"{name}.r1.trace").read_bytes()).hexdigest())

    spec = importlib.util.spec_from_file_location(
        "report_verifier", ROOT / "local_task/find-sqli-login/tests/test_security_report.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    with tempfile.TemporaryDirectory(prefix="e048-report-") as tmp:
        verifier.REPORT_PATH = Path(tmp) / "security_report.json"
        for n, action in actions("find-sqli-login"):
            if action.get("tool") != "write_file":
                continue
            verifier.REPORT_PATH.write_text(action["content"])
            failures = []
            for check in (verifier.test_security_report_exists,
                          verifier.test_security_report_is_valid_json_object,
                          verifier.test_report_contains_login_sql_injection_finding):
                try:
                    check()
                except AssertionError as exc:
                    failures.append(str(exc))
            print("find-sqli-login", n, "actual verifier failures:", failures)
            assert failures, "Archived candidate unexpectedly passes: revisit diagnosis"

    for name in ("fix-sqli-login", "fix-sqli-search"):
        original = ast.dump(ast.parse(
            (ROOT / f"local_task/{name}/environment/app/main.py").read_text()))
        for n, action in actions(name):
            if action.get("tool") != "write_file":
                continue
            same = ast.dump(ast.parse(action["content"])) == original
            print(name, n, "unchanged main.py AST:", same)
            assert same and action["path"] == "/app/main.py"

    expected = sorted((ROOT / "local_task/incident-log-forensics/tests/expected_incident_report.txt")
                      .read_text().splitlines())
    for n, action in actions("incident-log-forensics"):
        if action.get("tool") != "write_file":
            continue
        same = sorted(action["content"].splitlines()) == expected
        print("incident-log-forensics", n, "normalized exact match:", same)
        assert not same, "Archived candidate unexpectedly matches: revisit diagnosis"


if __name__ == "__main__":
    main()
