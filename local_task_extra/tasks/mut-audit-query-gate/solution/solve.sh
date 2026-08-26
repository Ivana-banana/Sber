#!/bin/bash
set -euo pipefail
cat >/workspace/review/findings.json <<'JSON'
{"observations":[{"kind":"sql_injection","component":"portal/query_gate.py:authorize","proof":"authorize interpolates login_name and pin into statement before sqlite executes it.","remedy":"Use SQL placeholders and pass both values separately."}]}
JSON
