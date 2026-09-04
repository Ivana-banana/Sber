#!/bin/sh
# Bring up the local model endpoint on the isolated eval network.
#
# The model runs as a container rather than on the host for one reason: the host's
# firewall drops traffic from docker bridges to the host, and the alternatives are
# both worse than a container. Rebinding the host's model service to 0.0.0.0 changes
# a system service outside this repository; `--network host` for task containers would
# let a task's own service bind host ports and collide with the next repeat.
#
# The store is mounted read-only: this is the machine owner's model directory, and an
# eval harness has no business writing to it.
set -eu

NETWORK=${NETWORK:-sber-eval}
NAME=${NAME:-sber-model}
STORE=${STORE:-/var/lib/ollama}

docker network inspect "$NETWORK" >/dev/null 2>&1 || \
  docker network create --internal --subnet 172.28.0.0/24 --gateway 172.28.0.1 "$NETWORK"

docker rm -f "$NAME" >/dev/null 2>&1 || true

# The GPU is offered when the host has one. This card holds 4 GiB, and the 7B Q4_K_M
# weights are 4.68 GiB, so nothing here promises a full offload: ollama places the
# layers that fit and runs the rest on the CPU. That is still the difference between
# a stand whose latency is admittedly unrepresentative and one that is merely slow.
GPU_FLAGS=""
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  GPU_FLAGS="--gpus all"
fi

docker run -d --name "$NAME" --network "$NETWORK" $GPU_FLAGS \
  -v "$STORE:/models:ro" \
  -e OLLAMA_MODELS=/models \
  -e OLLAMA_MAX_LOADED_MODELS=1 \
  -e OLLAMA_KEEP_ALIVE=30m \
  ollama/ollama:latest >/dev/null

printf 'waiting for %s' "$NAME"
i=0
while [ "$i" -lt 60 ]; do
  if docker exec "$NAME" ollama list >/dev/null 2>&1; then
    printf '\nendpoint: http://%s:11434/v1\n' "$NAME"
    docker exec "$NAME" ollama list
    exit 0
  fi
  printf '.'
  sleep 2
  i=$((i + 1))
done
printf '\n%s did not answer in 120s\n' "$NAME" >&2
docker logs --tail 20 "$NAME" >&2
exit 1
