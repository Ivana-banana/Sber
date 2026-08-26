#!/bin/bash
set -euo pipefail
cat > /app/incident_report.txt <<'EOF'
attacker_ip=198.51.100.44
compromised_user=deploy
first_success_utc=2026-06-04T10:03:08.902Z
EOF
