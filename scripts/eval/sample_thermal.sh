#!/bin/sh
# Sample what the machine is doing to itself while a suite runs.
#
# E033, E034 and E035 are the same code twice over and their steady-state call cost
# rose 7.5 -> 8.1 -> 8.7 s. At the end of that sequence the CPU read 95.4 C with 1.5 GB
# of 15 GB free, so absolute seconds are not comparable between runs hours apart and
# the ablation has to be judged on the normalised first call instead. This records the
# evidence rather than leaving it as an inference.
#
#   sh scripts/eval/sample_thermal.sh build/thermal-E036.log &
set -eu
OUT=${1:-thermal.log}
INTERVAL=${2:-60}
printf 'time\ttemp_c\tload1\tmem_avail_mb\tfreq_mhz\n' > "$OUT"
while :; do
  TEMP=$(sensors 2>/dev/null | awk '/Tctl/ {gsub(/[+C°]/,"",$2); print $2; exit}')
  LOAD=$(awk '{print $1}' /proc/loadavg)
  MEM=$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo)
  FREQ=$(awk '/cpu MHz/ {sum+=$4; n++} END {if (n) printf "%d", sum/n}' /proc/cpuinfo)
  printf '%s\t%s\t%s\t%s\t%s\n' "$(date +%H:%M:%S)" "${TEMP:--}" "$LOAD" "$MEM" "${FREQ:--}" >> "$OUT"
  sleep "$INTERVAL"
done
