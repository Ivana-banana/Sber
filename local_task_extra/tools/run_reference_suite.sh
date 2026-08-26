#!/bin/bash
set -uo pipefail
SELF_DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$SELF_DIR/../tasks" && pwd)
REPORT="$SELF_DIR/../validation/reference-results.tsv"
LOG_DIR="$SELF_DIR/../validation/runtime-logs"
rm -rf "$LOG_DIR"
mkdir -p "$LOG_DIR"
printf 'task\tresult\tdetail\n' >"$REPORT"

run_one() {
  local name="$1" target=/app service=0 task="$ROOT/$1"
  case "$name" in
    mut-audit-query-gate) target=/workspace/review ;;
    mut-forensics-egress-ledger) target=/var/lib/case-42 ;;
    ctf-header-override|ctf-signed-cookie|ctf-duplicate-parameter) service=1 ;;
  esac
  sudo -n pkill -f '^python3 /app/server.py$' >/dev/null 2>&1 || true
  sudo -n rm -rf /app /workspace/review /srv/bootstrap /opt/runstate /var/lib/case-42 /logs/verifier
  sudo -n mkdir -p "$target" /logs/verifier
  case "$name" in
    mut-greeting-farewell) sudo -n mkdir -p /srv/bootstrap ;;
    mut-salutation-beacon) sudo -n mkdir -p /opt/runstate ;;
  esac
  sudo -n cp -a "$task/environment/." "$target/"
  if [[ "$service" == 1 ]]; then
    sudo -n python3 /app/server.py >"$LOG_DIR/service-${name}.log" 2>&1 &
    local server_pid=$!
    sleep 0.25
  fi
  local solution_status test_status reward
  if sudo -n bash "$task/solution/solve.sh" >"$LOG_DIR/solution-${name}.log" 2>&1; then
    solution_status=ok
  else
    solution_status=fail
  fi
  if sudo -n bash "$task/tests/test.sh" >"$LOG_DIR/test-${name}.log" 2>&1; then
    test_status=ok
  else
    test_status=fail
  fi
  reward=$(sudo -n cat /logs/verifier/reward.txt 2>/dev/null || printf 'missing')
  if [[ "$service" == 1 ]]; then
    sudo -n kill "$server_pid" >/dev/null 2>&1 || true
    sudo -n wait "$server_pid" >/dev/null 2>&1 || true
  fi
  if [[ "$solution_status" == ok && "$test_status" == ok && "$reward" == 1 ]]; then
    printf '%s\tPASS\treward=1\n' "$name" | tee -a "$REPORT"
  else
    printf '%s\tFAIL\tsolution=%s,test=%s,reward=%s\n' "$name" "$solution_status" "$test_status" "$reward" | tee -a "$REPORT"
  fi
}

for d in "$ROOT"/*; do
  run_one "$(basename "$d")"
done

if grep -q $'\tFAIL\t' "$REPORT"; then
  exit 1
fi
