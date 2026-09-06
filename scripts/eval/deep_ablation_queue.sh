#!/bin/sh
# The 2026-09-07 deep ablation queue: eight arms against one frozen base.
#
# Plan: docs/superpowers/plans/2026-09-07-deep-ablation-queue.md, section 5.
# Prediction, written before the first run: docs/research/live-runs/deep-queue-prediction.md
#
# Two blocks, in this order and not the other one.
#
# Block 1 is the six organiser tasks -- the metric the project is actually ranked on --
# with a control run of the base at the start and another at the end. The controls
# bracket the arms rather than sitting at one end because the stand drifts: the machine
# heats, the model reloads, and a difference measured against a control from six hours
# earlier cannot be told from that drift.
#
# Block 2 is the eighteen local fixtures. Same archives, more resolution: six binary
# tasks cannot separate a real gain from one lucky task, and the fixture set is where a
# regression against an already-won task shows up. It runs second because the six
# organiser tasks are the ones that must be measured even if the night ends early.
#
# Sequential on purpose: the suite and the model share one GPU, and two suites at once
# would measure contention instead of the factor. Every run appends its row as it
# finishes, and a label already in the summary is skipped -- so an interrupted queue
# resumes instead of replaying.
set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$here/../.." || exit 1

M7=qwen2.5-coder:7b-instruct-q4_K_M
COMMON="--base-url http://sber-proxy:8000/v1 --api-key ollama --counter-container sber-proxy --timeout-scale 1.0"
PUBLIC=local_task
FIXTURES=local_task_extra/tasks
SUMMARY=build/deep-queue-summary.tsv

[ -f "$SUMMARY" ] || printf 'label\tset\tsolved\twrote\tcalls\ttokens\ttok_per_call\twall_s\n' > "$SUMMARY"

summarise() {
  label=$1; set_name=$2
  python3 - "$label" "$set_name" "$SUMMARY" <<'PY'
import sys, pathlib
label, set_name, out = sys.argv[1], sys.argv[2], sys.argv[3]
path = pathlib.Path(f"build/eval-{label}/results.tsv")
if not path.is_file():
    pathlib.Path(out).open("a").write(f"{label}\t{set_name}\tNO-RESULTS\t-\t-\t-\t-\t-\n")
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
    f"{label}\t{set_name}\t{solved}/{len(body)}\t{wrote}/{len(body)}\t"
    f"{int(calls)}\t{int(tokens)}\t{per}\t{int(wall)}\n"
)
PY
}

run() {
  label=$1; set_name=$2; archive=$3
  if cut -f1 "$SUMMARY" | grep -qx "$label"; then
    echo "=== $label already in $SUMMARY, skipped $(date -Is) ==="
    return 0
  fi
  if [ ! -f "$archive" ]; then
    echo "=== $label: archive $archive missing, skipped $(date -Is) ==="
    printf '%s\t%s\tNO-ARCHIVE\t-\t-\t-\t-\t-\n' "$label" "$set_name" >> "$SUMMARY"
    return 0
  fi
  case $set_name in
    public) root=$PUBLIC ;;
    *)      root=$FIXTURES ;;
  esac
  echo "=== $label ($set_name) start $(date -Is) ==="
  python3 scripts/eval/run_suite.py --tasks-root "$root" $COMMON \
    --label "$label" --out "build/eval-$label" --archive "$archive" --model "$M7" \
    > "build/$(echo "$label" | tr 'A-Z' 'a-z')-run.log" 2>&1
  echo "=== $label done rc=$? $(date -Is) ==="
  summarise "$label" "$set_name"
}

stand() {
  echo "=== stand at CONTEXT=4096 $(date -Is) ==="
  CONTEXT=4096 sh scripts/eval/start_model.sh || exit 1
  docker start sber-proxy >/dev/null 2>&1 || sh scripts/eval/start_proxy.sh || exit 1
  sleep 5
}

BASE=build/b-deep-base.zip
H01A=build/e062-h01a-signed-observation.zip
H01B=build/e063-h01b-read-pagination.zip
H03=build/e064-h03-hypothesis.zip
H04=build/e065-h04-task-block.zip
H05A=build/e066-h05a-audit-procedure.zip
H06B=build/e067-h06b-checkpoint-probe.zip
H08=build/e068-h08-content-novelty.zip
H09=build/e069-h09-verify-detail.zip

stand

# --------------------------------------------- block 1: the six organiser tasks -----
run B1-base-public          public "$BASE"
run E062-h01a-public        public "$H01A"
run E063-h01b-public        public "$H01B"
run E064-h03-public         public "$H03"
run E065-h04-public         public "$H04"
run E066-h05a-public        public "$H05A"
run E067-h06b-public        public "$H06B"
run E068-h08-public         public "$H08"
run E069-h09-public         public "$H09"
run B2-base-public-repeat   public "$BASE"

# ------------------------------------------------- block 2: the eighteen fixtures ---
# Ordered by how readable the arm's own mechanism is on this set, not by expected size
# of effect: the two confirmed controller defects first, then the two that change what
# the model is told at the moment it is stuck, then the prompt factors.
run B3-base-fixtures        fixtures "$BASE"
run E069-h09-fixtures       fixtures "$H09"
run E068-h08-fixtures       fixtures "$H08"
run E062-h01a-fixtures      fixtures "$H01A"
run E067-h06b-fixtures      fixtures "$H06B"
run E066-h05a-fixtures      fixtures "$H05A"
run E064-h03-fixtures       fixtures "$H03"
run E065-h04-fixtures       fixtures "$H04"
run E063-h01b-fixtures      fixtures "$H01B"
run B4-base-fixtures-repeat fixtures "$BASE"

echo "=== deep ablation queue complete $(date -Is) ==="
cat "$SUMMARY"
