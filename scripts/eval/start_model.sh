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
# Serving context window. 4096 is ollama's own default and is what every run up to
# E049 was measured under; it is kept as the default so that number stays comparable.
# It is a variable because it is a factor: the agent's prompt is about 3000 tokens by
# `OBSERVATION_BUDGET_CHARS` plus the system block, and `LLM_MAX_OUTPUT_TOKENS` is
# 2048, so 4096 cannot hold both and the server truncates without saying so.
CONTEXT=${CONTEXT:-4096}

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

start_model() {
  docker run -d --name "$NAME" --network "$NETWORK" $1 \
    -v "$STORE:/models:ro" \
    -e OLLAMA_MODELS=/models \
    -e OLLAMA_MAX_LOADED_MODELS=1 \
    -e OLLAMA_KEEP_ALIVE=30m \
    -e OLLAMA_CONTEXT_LENGTH="$CONTEXT" \
    ollama/ollama:latest >/dev/null
}

# `--gpus all` asks the toolkit for a CDI device, and the generated spec at
# /etc/cdi/nvidia.yaml goes stale whenever the host's driver libraries are
# upgraded without regenerating it: the container then dies at create time on a
# mount for a file that no longer exists. That is a host problem needing root,
# so instead of silently falling back to the CPU -- which is the difference
# between a run that finishes and a run that is killed at its deadline -- ask
# the same runtime the legacy way, which resolves libraries through ldcache.
if ! start_model "$GPU_FLAGS" 2>/tmp/start_model.$$; then
  if [ -n "$GPU_FLAGS" ]; then
    sed 's/^/  /' /tmp/start_model.$$ >&2
    printf 'CDI request failed; retrying through the legacy nvidia runtime\n' >&2
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    GPU_FLAGS="--runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=compute,utility"
    start_model "$GPU_FLAGS" || { rm -f /tmp/start_model.$$; exit 1; }
  else
    cat /tmp/start_model.$$ >&2
    rm -f /tmp/start_model.$$
    exit 1
  fi
fi
rm -f /tmp/start_model.$$
printf 'gpu flags: %s\n' "${GPU_FLAGS:-none}"

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
