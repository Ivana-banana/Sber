#!/bin/bash
set -euo pipefail
cat > /app/audit_report.json <<'JSON'
{"findings":[{"category":"hardcoded_secret","location":"client.py:6","evidence":"RELAY_API_TOKEN is assigned sk_live_coral_7nJ4Z3m9Q in source.","severity":"high","recommendation":"Load the token from a protected runtime secret instead of source control."}]}
JSON
