#!/bin/sh
# The 2026-09-08 night: two arms aimed at the one failure the traces actually show.
#
# Both arms exist because of A20260907c and the N1 pair, not because of a code smell:
# across thirty saved SQLi runs the handlers directory was listed every time and opened
# never, and the budget replay shows a first call of 85.731 s handing the second one a
# 4.269 s ceiling with 389.869 s still spendable. N2 removes that clamp; N3 names the
# modules the entry point reaches. The eight cosmetic arms of 2026-09-07 are not here:
# they were measured, and none of them gained a task.
#
# Ordering rules, unchanged from the previous queue and for the same reasons.
#
# Public first. It is the metric the project is ranked on, and it is the block that
# must exist even if the night ends early.
#
# Controls bracket AND interleave. The stand drifts -- the machine heats, the model
# reloads -- so B sits at both ends and once in the middle, and the two arms swap order
# between their repeats. A difference measured against a control from six hours earlier
# cannot be told from that drift.
#
# Two repeats per arm on public. Six binary tasks cannot separate a real gain from one
# lucky task, and E062-E069 proved single runs at this scale are unreadable: the base
# itself moved between 4/18 and 5/18 on the same archive.
#
# Fixtures last and once each. They are our hypothesis about the shape of the closed
# set, not the ranked metric; they are here as the regression net with more resolution.
#
# Sequential on purpose: the suite and the model share one GPU. Every run appends its
# row as it finishes, and a label already in the summary is skipped, so an interrupted
# queue resumes instead of replaying.
set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$here/../.." || exit 1

M7=qwen2.5-coder:7b-instruct-q4_K_M
COMMON="--base-url http://sber-proxy:8000/v1 --api-key ollama --counter-container sber-proxy --timeout-scale 1.0"
PUBLIC=local_task
FIXTURES=local_task_extra/tasks
SUMMARY=build/night-2026-09-08-summary.tsv

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

BASE=build/b-night-base.zip
N2=build/n2-phase-budget.zip
N3=build/n3-source-index.zip

stand

# ------------------------------------------------ block 1: the six organiser tasks --
run B1-base-public        public "$BASE"
run N2-1-public           public "$N2"
run N3-1-public           public "$N3"
run B2-base-public        public "$BASE"
run N3-2-public           public "$N3"
run N2-2-public           public "$N2"
run B3-base-public        public "$BASE"

# ---------------------------------------------------- block 2: the eighteen fixtures
run B4-base-fixtures      fixtures "$BASE"
run N2-fixtures           fixtures "$N2"
run N3-fixtures           fixtures "$N3"
run B5-base-fixtures      fixtures "$BASE"

echo "=== night queue complete $(date -Is) ==="
cat "$SUMMARY"
