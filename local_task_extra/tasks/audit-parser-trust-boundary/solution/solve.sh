#!/bin/bash
set -euo pipefail
cat > /app/audit_report.json <<'JSON'
{"findings":[{"category":"unsafe_deserialization","location":"ingest.py:decode_manifest","evidence":"The request flow gateway.handle_upload passes partner body to pickle.loads without authentication or safe decoding.","impact":"A crafted pickle can execute code while it is deserialized.","recommendation":"Use a data-only format with schema validation instead of pickle."},{"category":"command_injection","location":"hooks.py:apply_hook","evidence":"apply_hook concatenates hook_name and manifest name into command and invokes subprocess.run with shell=True.","impact":"An attacker can inject shell syntax through request-controlled fields.","recommendation":"Select hooks from an allowlist and invoke subprocess with an argument list and shell disabled."}]}
JSON
