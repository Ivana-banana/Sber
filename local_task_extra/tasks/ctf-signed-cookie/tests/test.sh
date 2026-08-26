#!/bin/bash
set -uo pipefail
REWARD=/logs/verifier/reward.txt
mkdir -p "$(dirname "$REWARD")"
if python3 "$(dirname "$0")/check.py"; then
  printf '1\n' >"$REWARD"
else
  printf '0\n' >"$REWARD"
fi
exit 0
