#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/ubuntu/acp_audit
FS="$ROOT/rootfs_amd64"
RAW="$ROOT/raw"

sudo chroot "$FS" /bin/sh -c '
set +e
printf "=== identity ===\n"
id
printf "=== os-release ===\n"
cat /etc/os-release
printf "=== Python executables and versions ===\n"
for p in /usr/local/bin/python /usr/local/bin/python3 /usr/local/bin/python3.12 /app/.venv/bin/python /app/.venv/bin/python3 /app/.venv/bin/python3.12; do
  if [ -e "$p" ]; then
    printf "%s -> " "$p"
    readlink -f "$p" 2>/dev/null || printf "not-a-symlink"
    printf "%s version: " "$p"
    "$p" --version
  else
    printf "%s: ABSENT\n" "$p"
  fi
done
printf "=== Tool probes ===\n"
for tool in git curl wget rg ripgrep grep jq sqlite3 unzip zip tar patch gcc make gdb objdump readelf strings file nmap nc netcat tcpdump tshark openssl node java php docker podman; do
  printf '%s\n' "--- $tool ---"
  path=$(command -v "$tool")
  status=$?
  if [ "$status" -ne 0 ]; then
    printf "ABSENT\n"
    continue
  fi
  printf "PATH=%s\n" "$path"
  case "$tool" in
    git) git --version ;;
    curl) curl --version | head -n 1 ;;
    wget) wget --version | head -n 1 ;;
    rg) rg --version | head -n 1 ;;
    ripgrep) ripgrep --version | head -n 1 ;;
    grep) grep --version | head -n 1 ;;
    jq) jq --version ;;
    sqlite3) sqlite3 --version ;;
    unzip) unzip -v | head -n 3 | tail -n 1 ;;
    zip) zip -v | head -n 3 | tail -n 1 ;;
    tar) tar --version | head -n 1 ;;
    patch) patch --version | head -n 1 ;;
    gcc) gcc --version | head -n 1 ;;
    make) make --version | head -n 1 ;;
    gdb) gdb --version | head -n 1 ;;
    objdump) objdump --version | head -n 1 ;;
    readelf) readelf --version | head -n 1 ;;
    strings) strings --version | head -n 1 ;;
    file) file --version | head -n 1 ;;
    nmap) nmap --version | head -n 1 ;;
    nc|netcat) "$tool" -h 2>&1 | head -n 1 ;;
    tcpdump) tcpdump --version 2>&1 | head -n 1 ;;
    tshark) tshark --version | head -n 1 ;;
    openssl) openssl version ;;
    node) node --version ;;
    java) java -version 2>&1 | head -n 1 ;;
    php) php --version | head -n 1 ;;
    docker) docker --version ;;
    podman) podman --version ;;
  esac
  printf "exit=%s\n" "$?"
done
' > "$RAW/base_inventory.txt" 2>&1

sudo chroot "$FS" /bin/sh -c '
for p in /usr/local/bin/python3.12 /app/.venv/bin/python; do
  if [ -x "$p" ]; then
    printf "=== %s -m pip list ===\n" "$p"
    "$p" -m pip list
    printf "=== %s -m pip freeze ===\n" "$p"
    "$p" -m pip freeze
  else
    printf "=== %s ABSENT ===\n" "$p"
  fi
done
exit 0
' > "$RAW/pip_inventory.txt" 2>&1

cp "$ROOT/image_metadata_inventory.py" "$FS/tmp/image_metadata_inventory.py"
sudo chroot "$FS" /bin/sh -c '
for p in /usr/local/bin/python3.12 /app/.venv/bin/python; do
  if [ -x "$p" ]; then
    printf "=== %s distribution metadata ===\n" "$p"
    "$p" /tmp/image_metadata_inventory.py
  else
    printf "=== %s ABSENT ===\n" "$p"
  fi
done
exit 0
' > "$RAW/python_distribution_metadata.txt" 2>&1
rm -f "$FS/tmp/image_metadata_inventory.py"

sudo chroot "$FS" /bin/sh -c '
find /app /opt /usr/local/lib -maxdepth 8 -type f \( -iname "*agent*" -o -iname "*template*" -o -iname "*example*" -o -iname "*sample*" \) -printf "%p\n" 2>/dev/null | sort
' > "$RAW/agent_related_files.txt" 2>&1
