#!/bin/bash
set -euo pipefail
cat >/var/lib/case-42/conclusion.env <<'EOF'
client_ip=198.51.100.172
principal=lena
object_name=partner-ledger-q2
logical_bytes=5242880
event_time=2026-07-03T19:11:42.007Z
EOF
