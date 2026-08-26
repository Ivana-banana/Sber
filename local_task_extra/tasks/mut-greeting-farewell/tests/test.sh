#!/bin/bash
set -uo pipefail
mkdir -p /logs/verifier
if python3 "$(dirname "$0")/check.py"; then printf '1\n' >/logs/verifier/reward.txt; else printf '0\n' >/logs/verifier/reward.txt; fi
exit 0
