#!/bin/sh
# Put a token counter between the agent and the model.
#
# The agent reports its own total on the last line it prints, and a run killed at its
# deadline never prints it -- which in E009 was most rows. The counter does not depend
# on how the run ends, and disagreement between the two numbers is itself a finding.
set -eu

NETWORK=${NETWORK:-sber-eval}
NAME=${NAME:-sber-proxy}
UPSTREAM=${UPSTREAM:-http://sber-model:11434}
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --network "$NETWORK" \
  -v "$HERE:/opt/eval:ro" \
  python:3.12-slim \
  python3 /opt/eval/counting_proxy.py --upstream "$UPSTREAM" >/dev/null

i=0
while [ "$i" -lt 30 ]; do
  if docker exec "$NAME" python3 -c \
      "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/__counters',timeout=5)" \
      >/dev/null 2>&1; then
    printf 'endpoint through counter: http://%s:8000/v1\n' "$NAME"
    exit 0
  fi
  sleep 1
  i=$((i + 1))
done
printf '%s did not answer\n' "$NAME" >&2
docker logs --tail 20 "$NAME" >&2
exit 1
