#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/ubuntu/acp_audit
RAW="$ROOT/raw"
BLOBS="$ROOT/blobs"
FS="$ROOT/rootfs_amd64"
rm -rf "$FS"
mkdir -p "$FS"

while IFS= read -r digest; do
  archive="$BLOBS/${digest#sha256:}.tar.gz"
  tar -tzf "$archive" | while IFS= read -r member; do
    member=${member#./}
    base=${member##*/}
    parent=${member%/*}
    [ "$parent" = "$member" ] && parent=''
    if [ "$base" = '.wh..wh..opq' ]; then
      if [ -d "$FS/$parent" ]; then
        find "$FS/$parent" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
      fi
    elif [[ "$base" == .wh.* ]]; then
      rm -rf -- "$FS/$parent/${base#.wh.}"
    fi
  done
  tar -xzf "$archive" -C "$FS" --no-same-owner --delay-directory-restore \
    --exclude='*/.wh..wh..opq' --exclude='*/.wh.*'
done < "$RAW/amd64_layer_digests.txt"

du -sb "$FS" | tee "$RAW/rootfs_unpacked_size_bytes.txt"
find "$FS" -xdev -type f \( -name 'python' -o -name 'python[0-9]*' -o -name 'python[0-9]*.[0-9]*' \) -printf '%p\n' | sort | tee "$RAW/python_executables_found.txt"
find "$FS" -xdev -type f \( -name 'pip' -o -name 'pip[0-9]*' \) -printf '%p\n' | sort | tee "$RAW/pip_executables_found.txt"
