#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/ubuntu/acp_audit
FS="$ROOT/rootfs_amd64"
RAW="$ROOT/raw"

{
  printf 'compressed_layer_sum_bytes='
  awk -F '"size": ' '/"size": [0-9]+/ {sub(/,.*/, "", $2); sum += $2} END {printf "%.0f\n", sum}' "$RAW/amd64_manifest.json"
  printf 'unpacked_rootfs_bytes='
  cut -f1 "$RAW/rootfs_unpacked_size_bytes.txt"
  printf 'unpacked_rootfs_human='
  du -sh "$FS" | cut -f1
  printf 'blob_files_bytes='
  du -sb "$ROOT/blobs" | cut -f1
} > "$RAW/sizes.txt"

sudo chroot "$FS" /bin/sh -c '
export PATH=/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
printf "=== configured path ===\n%s\n" "$PATH"
printf "=== direct command versions ===\n"
git --version
curl --version | head -n 1
wget --version | head -n 1
rg --version | head -n 1
grep --version | head -n 1
jq --version
unzip -v | head -n 1
zip -v | grep -m 1 "This is Zip"
tar --version | head -n 1
patch --version | head -n 1
gcc --version | head -n 1
make --version | head -n 1
cmake --version | head -n 1
objdump --version | head -n 1
readelf --version | head -n 1
strings --version | head -n 1
file --version | head -n 1
tcpdump --version 2>&1 | head -n 1
openssl version
nc -h 2>&1 | head -n 1
printf "=== installed debian package versions ===\n"
dpkg-query -W -f="\${binary:Package}\t\${Version}\n" ca-certificates binutils build-essential cmake curl diffutils dnsutils fd-find file git grep iproute2 iputils-ping jq less locales netcat-openbsd openssl patch procps ripgrep tcpdump traceroute tree unzip wget zip 2>&1 | sort
printf "=== expected command state (name: command -v result) ===\n"
for tool in sqlite3 gdb nmap tshark node java php docker podman; do
  if command -v "$tool" >/dev/null 2>&1; then
    printf "%s\tPRESENT\t%s\n" "$tool" "$(command -v "$tool")"
  else
    printf "%s\tABSENT\n" "$tool"
  fi
done
' > "$RAW/exact_versions.txt" 2>&1
