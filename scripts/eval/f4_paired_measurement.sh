#!/bin/sh
# F4 paired measurement: run the public six-task suite twice, once on the control
# archive (no F4) and once on the treatment archive (F4), on one unchanged stand.
#
# F4 lifts the 90s pre-checkpoint call clamp once a slow/thinking endpoint is observed
# (agent/state.py `allow_long_llm`, wired in loop.py and macro.py). The clean one-factor
# pair is the F4 commit and its parent:
#   control  (no F4): commit 64636f9
#   treatment (F4)  : commit 8b568ac
# Build each with scripts/build_submission.py from its checkout, then pass both here.
#
# The measurement must run on TWO stands, both at --timeout-scale 1.0:
#   30B thinking (RTX 4080, qwen3:30b-a3b, CONTEXT=16384) -- where F4 should help:
#       the L-Q3-FIX 4/6 run had incident-log-forensics die on the TIME budget and its
#       thinking calls truncated at ~90s; F4 gives them the full budget. Expectation:
#       fewer/zero "lower bound" call marks, incident stop moves off `budget`, +0..1 solved.
#   7B (this laptop or the 4080, qwen2.5-coder:7b, CONTEXT=4096) -- the regression guard:
#       a fast endpoint never sets allow_long_llm, so behaviour must be byte-for-byte the
#       clamp path. CRITICAL: solved and deliverable count must NOT fall vs control.
#
# Nothing here is causal on its own; it produces the rows. Read them against the gates in
# docs/research/live-runs/2026-09-14-F4-paired-measurement-plan.md and record a card.
set -eu

CONTROL_ZIP=${1:?usage: f4_paired_measurement.sh <control-nof4.zip> <treatment-f4.zip>}
TREATMENT_ZIP=${2:?usage: f4_paired_measurement.sh <control-nof4.zip> <treatment-f4.zip>}

# Stand wiring -- set to the operator's live proxy/model before running.
MODEL=${MODEL:?set MODEL, e.g. qwen3:30b-a3b (30B stand) or qwen2.5-coder:7b-instruct-q4_K_M (7B)}
PROXY=${PROXY:?set PROXY to the counting-proxy container name on the eval network}
BASE_URL=${BASE_URL:-http://$PROXY:8000/v1}
API_KEY=${API_KEY:-ollama}
NETWORK=${NETWORK:-sber-eval}
TASKS_ROOT=${TASKS_ROOT:-local_task}
REPEATS=${REPEATS:-3}
TAG=${TAG:-$(date +%Y%m%d-%H%M%S)}

HERE=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$HERE"
mkdir -p build

run_one() {
  side=$1; zip=$2
  [ -f "$zip" ] || { printf 'archive not found: %s\n' "$zip" >&2; exit 1; }
  out="build/f4-paired-$TAG-$side"
  printf '\n=== %s === %s (%s repeats) on %s\n' "$side" "$zip" "$REPEATS" "$MODEL" >&2
  python3 scripts/eval/run_suite.py \
    --tasks-root "$TASKS_ROOT" \
    --archive "$zip" \
    --model "$MODEL" \
    --base-url "$BASE_URL" --api-key "$API_KEY" \
    --network "$NETWORK" --counter-container "$PROXY" \
    --timeout-scale 1.0 --repeats "$REPEATS" \
    --out "$out" --label "f4-paired-$TAG-$side"
  printf 'summary: %s/results.tsv\n' "$out" >&2
  sha256sum "$out/results.tsv" 2>/dev/null || true
}

# Control first, then treatment, on the same warm stand, so a stand drift shows up as
# control!=control across a re-run rather than being charged to F4.
run_one control "$CONTROL_ZIP"
run_one treatment "$TREATMENT_ZIP"

printf '\nDone. Compare solved / deliverable / tokens / wall per task, control vs treatment.\n' >&2
printf 'On the 7B stand solved and deliverable must not fall. Record a card before drawing a verdict.\n' >&2
