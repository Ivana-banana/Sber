#!/bin/sh
# Night queue. Sequential on purpose: the suite and the model share one GPU, and two
# suites at once would measure contention instead of the factor.
#
# The queue is ordered by the stand, not by importance. Changing the serving context
# window means restarting the model, and a restart between two arms of one comparison
# would be a second factor -- so every run at one window happens in one block, and the
# blocks are ordered 4096 (the window all 27 runs so far were measured under), then
# 8192, then 16384.
#
# Every run appends its own row to build/night-summary.tsv as it finishes. That file is
# the morning's starting point: it exists even if the queue is interrupted, and reading
# it costs nothing compared with re-deriving the same numbers from the logs.
set -u

# Repository root from this script's own location, never an absolute path from one
# machine: the rule the agent obeys applies to its harness too.
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$here/../.." || exit 1

M7=qwen2.5-coder:7b-instruct-q4_K_M
M3=qwen2.5-coder:3b-instruct-q4_K_M
COMMON="--base-url http://sber-proxy:8000/v1 --api-key ollama --counter-container sber-proxy --timeout-scale 1.0"
FIX=local_task_extra/tasks
SUMMARY=build/night-summary.tsv

[ -f "$SUMMARY" ] || printf 'label\tctx\tsolved\twrote\tcalls\ttokens\ttok_per_call\twall_s\n' > "$SUMMARY"

# One row per run, computed here so nobody has to re-derive it in the morning.
# Tokens per call uses ep_tokens/ep_calls: the `tokens` column is empty for any task
# killed at its deadline, and averaging over an empty column is how you get a lie.
summarise() {
  label=$1; ctx=$2
  python3 - "$label" "$ctx" "$SUMMARY" <<'PY'
import sys, pathlib
label, ctx, out = sys.argv[1], sys.argv[2], sys.argv[3]
path = pathlib.Path(f"build/eval-{label}/results.tsv")
if not path.is_file():
    pathlib.Path(out).open("a").write(f"{label}\t{ctx}\tNO-RESULTS\t-\t-\t-\t-\t-\n")
    raise SystemExit(0)
rows = [r.split("\t") for r in path.read_text().strip().split("\n")]
head, body = rows[0], rows[1:]
col = {name: i for i, name in enumerate(head)}
def num(row, name):
    try: return float(row[col[name]])
    except (KeyError, ValueError, IndexError): return 0.0
solved = sum(1 for r in body if r[col["reward"]] == "1")
wrote = sum(1 for r in body if r[col["wrote"]] == "yes")
calls = sum(num(r, "ep_calls") for r in body)
tokens = sum(num(r, "ep_tokens") for r in body)
wall = sum(num(r, "wall_s") for r in body)
per = round(tokens / calls, 1) if calls else 0
pathlib.Path(out).open("a").write(
    f"{label}\t{ctx}\t{solved}/{len(body)}\t{wrote}/{len(body)}\t"
    f"{int(calls)}\t{int(tokens)}\t{per}\t{int(wall)}\n"
)
PY
}

run() {
  label=$1; ctx=$2; shift 2
  # Resume. A queue that has to be restarted at 03:00 must not replay the six hours it
  # already spent: a label with a row in the summary is done. This is also why the row
  # is written by the run that produced it and never in a batch at the end.
  if cut -f1 "$SUMMARY" | grep -qx "$label"; then
    echo "=== $label already in $SUMMARY, skipped $(date -Is) ==="
    return 0
  fi
  echo "=== $label (ctx=$ctx) start $(date -Is) ==="
  python3 scripts/eval/run_suite.py --tasks-root "$FIX" $COMMON \
    --label "$label" --out "build/eval-$label" "$@" \
    > "build/$(echo "$label" | cut -d- -f1 | tr 'A-Z' 'a-z')-run.log" 2>&1
  echo "=== $label done rc=$? $(date -Is) ==="
  summarise "$label" "$ctx"
}

# The probes run inside the eval network and talk to the model directly. No proxy:
# they read `usage` out of the reply themselves, and borrowing the suite's counter
# would reset it under a run that is about to use it. `sber-eval` is an internal
# network, so a published host port is not dependable here -- a container on the
# network is.
probe() {
  label=$1
  if [ -s "docs/research/live-runs/$label.jsonl" ]; then
    echo "=== probe $label already done, skipped $(date -Is) ==="
    return 0
  fi
  echo "=== probe $label start $(date -Is) ==="
  docker run --rm --network sber-eval -v "$PWD/scripts/eval:/opt/eval:ro" \
    python:3.12-slim python3 /opt/eval/probe_context.py \
    --base-url http://sber-model:11434/v1 --model "$M7" \
    > "docs/research/live-runs/$label.jsonl" 2>&1
  echo "=== probe $label done rc=$? $(date -Is) ==="
}

# ollama warns when it truncates a prompt. We have never looked -- the container was
# always removed before anyone thought to. Harvest before every restart wipes it.
harvest_logs() {
  docker logs sber-model > "build/ollama-$1.log" 2>&1 || true
  echo "$1 ollama truncation lines: $(grep -ic truncat "build/ollama-$1.log" || true)"
}

stand() {
  echo "=== stand at CONTEXT=$1 $(date -Is) ==="
  CONTEXT=$1 sh scripts/eval/start_model.sh || exit 1
  docker start sber-proxy >/dev/null 2>&1 || sh scripts/eval/start_proxy.sh || exit 1
  sleep 5
}

# ---------------------------------------------------------------- block 1: 4096 -----
stand 4096
probe probe-context-4096
run E050-fixtures-baseline   4096 --archive build/e039-anchored-history.zip --model "$M7"
run E051-fixtures-repeat     4096 --archive build/e039-anchored-history.zip --model "$M7"
run E051b-fixtures-repeat3   4096 --archive build/e039-anchored-history.zip --model "$M7"
run E054-model-3b            4096 --archive build/e039-anchored-history.zip --model "$M3"
run E053a-macro-arm-loop     4096 --archive build/e044-macro.zip --model "$M7" --controller loop
run E053b-macro-arm-macro    4096 --archive build/e044-macro.zip --model "$M7" --controller macro
run E052-english-prompt      4096 --archive build/e042-english-prompt.zip --model "$M7"
harvest_logs 4096

# ---------------------------------------------------------------- block 2: 8192 -----
stand 8192
probe probe-context-8192
run E055-context-8192        8192 --archive build/e039-anchored-history.zip --model "$M7"
run E056-context-8192-repeat 8192 --archive build/e039-anchored-history.zip --model "$M7"
run E057-3b-at-8192          8192 --archive build/e039-anchored-history.zip --model "$M3"
run E058-macro-at-8192       8192 --archive build/e044-macro.zip --model "$M7" --controller macro
harvest_logs 8192

# ---------------------------------------------------------------- block 3: 16384 ----
# Third point on the curve. Last on purpose: it is the most likely to be cut short by
# morning, and losing the third point of a curve costs less than losing its middle.
stand 16384
probe probe-context-16384
run E059-context-16384       16384 --archive build/e039-anchored-history.zip --model "$M7"
harvest_logs 16384

# --------------------------------------------------------------------- restore ------
stand 4096
echo "=== queue complete $(date -Is) ==="
cat "$SUMMARY"
