#!/usr/bin/env python3
"""Can the model build the coupled fix when it does not have to locate or anchor it?

The measured fix-sqli runs half-repair: the query is parameterised, the call below
still executes it unbound, and the model never touches the consuming line. Two readings
fit: a capability ceiling on the coupled edit, or a loop artefact -- the model edits one
anchored line at a time via replace_text and cannot coordinate two.

This separates them offline, as the P2 report-vs-action probe did for comprehension.
The model is handed the whole vulnerable function and asked to return the corrected
whole function -- no locating, no anchoring. If it produces the bound form, the loop is
the blocker (steer toward a whole-function rewrite); if not, it is a ceiling.

Reads the function from the public task rather than embedding it. No eval stand needed
beyond an OpenAI-compatible endpoint; reuse the stand proxy to avoid a second model load.

Usage:
    python3 scripts/eval/probe_fix_construction.py \
        --base-url http://claude-e-proxy-20260912:8000/v1 --api-key ollama \
        --model qwen2.5-coder:7b-instruct-q4_K_M --repeats 3
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import urllib.request

HERE = pathlib.Path(__file__).resolve()
REPO = HERE.parents[2]
SOURCE = REPO / "local_task" / "fix-sqli-login" / "environment" / "app" / "routers" / "auth.py"

SYSTEM = "You are a senior Python engineer. Return only code, no prose."
USER = """\
This asyncpg handler has a SQL injection: it interpolates req.username and req.password
into the SQL string. Fix it with a parameterised query (asyncpg uses $1, $2 placeholders
and takes the values as extra arguments to fetchrow), keeping the behaviour identical.
Return the complete corrected `login` function only.

```python
{code}
```"""


def call(base: str, key: str, model: str, code: str) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": USER.format(code=code)}],
        "temperature": 0, "max_tokens": 2048,
    }).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


def grade(text: str) -> tuple[bool, dict]:
    # Coupled fix: $1/$2 placeholders, no f-string interpolation of the value, and the
    # values passed as arguments to fetchrow -- the consuming call changed with the query.
    placeholders = "$1" in text and "$2" in text
    no_interp = "{req.username}" not in text and "{req.password}" not in text
    params_passed = bool(re.search(r"fetch(?:row|val)?\s*\([^)]*req\.username[^)]*req\.password", text, re.S))
    return (placeholders and no_interp and params_passed,
            {"placeholders": placeholders, "no_interp": no_interp, "params_passed": params_passed})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://claude-e-proxy-20260912:8000/v1")
    ap.add_argument("--api-key", default="ollama")
    ap.add_argument("--model", default="qwen2.5-coder:7b-instruct-q4_K_M")
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    code = SOURCE.read_text()
    oks = []
    for i in range(args.repeats):
        text = call(args.base_url, args.api_key, args.model, code)
        ok, parts = grade(text)
        oks.append(ok)
        print(f"=== repeat {i} correct={ok} {parts} ===\n{text}\n")
    print("SUMMARY correct:", oks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
