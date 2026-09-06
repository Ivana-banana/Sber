#!/bin/sh
# Two extra hours, granted after the night queue finished. Only repeats: E052 and
# E053a each produced 4/18 from a single point, and a single point is what E042 already
# cost this project once. Same stand, same window (4096, where the night left it), same
# archives -- the repeats add points, not factors.
set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$here/../.." || exit 1

M7=qwen2.5-coder:7b-instruct-q4_K_M
COMMON="--base-url http://sber-proxy:8000/v1 --api-key ollama --counter-container sber-proxy --timeout-scale 1.0"
FIX=local_task_extra/tasks
SUMMARY=build/night-summary.tsv

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

# The night's last action already restored the stand to 4096; assert rather than restart.
docker ps --format '{{.Names}}' | grep -qx sber-model || { echo "stand down"; exit 1; }

run E052b-english-repeat  4096 --archive build/e042-english-prompt.zip --model "$M7"
run E053a2-macro-arm-loop-repeat 4096 --archive build/e044-macro.zip --model "$M7" --controller loop
run E052c-english-repeat3 4096 --archive build/e042-english-prompt.zip --model "$M7"
echo "=== morning queue complete $(date -Is) ==="
