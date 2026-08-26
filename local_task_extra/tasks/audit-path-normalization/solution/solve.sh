#!/bin/bash
set -euo pipefail
cat > /app/audit_report.json <<'JSON'
{"findings":[{"category":"path_traversal","location":"export_store.py:open_export","evidence":"open_export appends attacker-controlled requested_name to EXPORT_ROOT/account and reads it without containment validation.","impact":"A caller can read files outside its export directory with traversal components.","recommendation":"Resolve the candidate and require it to remain below the per-account directory."},{"category":"symlink_escape","location":"export_store.py:read_latest","evidence":"read_latest follows the privileged latest symlink without verifying that its resolved target remains inside the account directory.","impact":"A symlink can redirect a tenant read to an unrelated file.","recommendation":"Resolve the link and enforce an account-directory containment check before reading."}]}
JSON
