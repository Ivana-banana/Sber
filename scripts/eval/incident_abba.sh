#!/bin/sh
# 17.09.2026 evening: incident-only ABBA on the live stand, kt7.zip (0c96797) vs r2b.zip.
# The decision rule was written to JOURNAL.md (20:52) before any number existed.
# Runs against the main checkout's build/ directory; a block counts only when it is marked VALID.
cd "/home/m1hairu/Documents/ai challenge/Sber" || exit 1
export AGENT_TRACE_TOOLS=1 AGENT_TRACE_BUDGET=1
MODEL='hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ3_XXS'
LOG=build/logs/incident_abba.log
KT7=57c78a832192526fa59c656cca073d75f5c73e7f3a5acaa14bd99caad786857b
R2B=6b9ba85ccab6cadd4b39cfa4a0246303be7584521408aad52a54052c188c1a3d
RUNPID=0
foreign() { pgrep -f "run_suite.py" | grep -vx "$RUNPID" | wc -l; }
block() { label=$1; arch=$2; want=$3
  out="build/eval-$label"
  [ -e "$out/.block-complete" ] && { echo "=== $label already complete, skipped" >> $LOG; return; }
  rm -rf "$out"
  got=$(sha256sum "$arch" | cut -d' ' -f1)
  [ "$got" = "$want" ] || { echo "=== $label ABORT archive sha $got != expected" >> $LOG; exit 1; }
  RUNPID=0
  echo "=== $label start $(date +%H:%M:%S) load=$(cut -d' ' -f1-3 /proc/loadavg) arch=$(echo "$got" | cut -c1-8) runner=$(sha256sum scripts/eval/run_suite.py | cut -c1-8) foreign_runs=$(foreign)" >> $LOG
  python3 scripts/eval/run_suite.py --tasks-root local_task --only incident-log-forensics --model "$MODEL" --base-url http://sber-proxy:8000/v1 --api-key ollama --counter-container sber-proxy --repeats 3 --timeout-scale 1.0 --reuse-images --archive "$arch" --label "$label" --out "$out" > "build/logs/$label.log" 2>&1 &
  RUNPID=$!
  wait $RUNPID; rc=$?
  f=$(foreign)
  rows=$(awk -F'\t' 'NR>1{n++} END{print n+0}' "$out/results.tsv" 2>/dev/null)
  ok=$(grep -c "complete=True" "build/logs/$label.log")
  verdict=VALID
  [ "$rc" = 0 ] && [ "$rows" = 3 ] && [ "$ok" -ge 1 ] || verdict="INVALID(rc=$rc rows=$rows complete=$ok)"
  [ "$f" = 0 ] || verdict="INVALID(foreign run_suite at end)"
  [ "$verdict" = VALID ] && touch "$out/.block-complete"
  echo "=== $label done $(date +%H:%M:%S) $verdict $(awk -F'\t' 'NR>1{n++; s+=$3; t+=$4; w+=$8} END{print s"/"n" tokens="t" wall="w}' "$out/results.tsv" 2>/dev/null) load=$(cut -d' ' -f1-3 /proc/loadavg)" >> $LOG
}
# Grace period: the human was on the stand minutes ago; never start on top of someone else's run.
sleep "${ABBA_GRACE_S:-300}"
if [ "$(foreign)" != 0 ]; then
  echo "=== not started $(date +%H:%M:%S): a foreign run_suite is on the stand" >> $LOG
  exit 0
fi
# Pulse while the stand works, as its own process: it must not die with a caller's process group.
setsid sh -c 'while kill -0 "$1" 2>/dev/null; do touch .afk/heartbeat; sleep 240; done' pulse $$ >/dev/null 2>&1 &
block INC-K1 build/kt7.zip $KT7
block INC-R1 build/r2b.zip $R2B
block INC-R2 build/r2b.zip $R2B
block INC-K2 build/kt7.zip $KT7
echo "INCIDENT-ABBA-DONE $(date +%H:%M:%S)" >> $LOG
