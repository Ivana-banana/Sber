#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/ubuntu/acp_audit
RAW="$ROOT/raw"
BLOBS="$ROOT/blobs"
REPO='secureintelligent/acp'
REGISTRY='https://registry-1.docker.io'
mkdir -p "$RAW" "$BLOBS"

curl --fail --silent --show-error --location \
  'https://auth.docker.io/token?service=registry.docker.io&scope=repository:secureintelligent/acp:pull' \
  -o "$RAW/token.json"
TOKEN=$(sed -n 's/.*"token":"\([^"]*\)".*/\1/p' "$RAW/token.json")
test -n "$TOKEN"
AUTH_HEADER="Authorization: Bearer $TOKEN"
ACCEPT_INDEX='Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json'
ACCEPT_MANIFEST='Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'

curl --fail --silent --show-error --location \
  -H "$AUTH_HEADER" -H "$ACCEPT_INDEX" \
  --dump-header "$RAW/index.headers" \
  "$REGISTRY/v2/$REPO/manifests/latest" -o "$RAW/index_latest.json"

AMD64_MANIFEST=sha256:5a93249d3dfd44dbf2e83806f89ce2662a3e575c8ad0fb5c54feb03ec5626132
curl --fail --silent --show-error --location \
  -H "$AUTH_HEADER" -H "$ACCEPT_MANIFEST" \
  --dump-header "$RAW/amd64_manifest.headers" \
  "$REGISTRY/v2/$REPO/manifests/$AMD64_MANIFEST" -o "$RAW/amd64_manifest.json"

CONFIG=sha256:$(sed -n '1,10 s/.*"digest": "sha256:\([0-9a-f]*\)".*/\1/p' "$RAW/amd64_manifest.json")
test "$CONFIG" != 'sha256:'
curl --fail --silent --show-error --location \
  -H "$AUTH_HEADER" \
  "$REGISTRY/v2/$REPO/blobs/$CONFIG" -o "$RAW/amd64_config.json"
printf '%s\n' "$CONFIG" > "$RAW/amd64_config.digest"

grep -o '"digest": "sha256:[0-9a-f]*"' "$RAW/amd64_manifest.json" \
  | tail -n +2 \
  | sed 's/"digest": "//;s/"//' > "$RAW/amd64_layer_digests.txt"
while IFS= read -r digest; do
  file="$BLOBS/${digest#sha256:}.tar.gz"
  if [ ! -s "$file" ]; then
    curl --fail --silent --show-error --location \
      -H "$AUTH_HEADER" "$REGISTRY/v2/$REPO/blobs/$digest" -o "$file"
  fi
  sha256sum "$file"
done < "$RAW/amd64_layer_digests.txt" | tee "$RAW/amd64_layer_sha256.txt"

date --utc --iso-8601=seconds > "$RAW/fetched_at_utc.txt"
printf 'index digest: '; grep -i '^docker-content-digest:' "$RAW/index.headers" | tr -d '\r'
printf 'amd64 manifest digest: '; grep -i '^docker-content-digest:' "$RAW/amd64_manifest.headers" | tr -d '\r'
printf 'config digest: %s\n' "$CONFIG"
