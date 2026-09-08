#!/usr/bin/env bash
# Stop hook: перед тем как ход считается завершённым — pytest, если
# agent/tests/pyproject.toml менялись с последнего зелёного прогона.
# См. CLAUDE.md, "Тесты и доказательство результата".
set -uo pipefail

MARKER=".claude/.last-green-test-run"

if [ -f "$MARKER" ]; then
  CHANGED="$(find agent tests pyproject.toml -type f -newer "$MARKER" 2>/dev/null | head -1)"
  if [ -z "$CHANGED" ]; then
    exit 0
  fi
fi

OUTPUT="$(python3 -m pytest -q 2>&1)"
STATUS=$?

if [ "$STATUS" -eq 0 ]; then
  touch "$MARKER"
  exit 0
fi

TAIL="$(printf '%s' "$OUTPUT" | tail -n 40)"
jq -n --arg out "$TAIL" \
  '{decision: "block", reason: ("pytest упал перед завершением хода:\n" + $out)}'

exit 0
