#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/ubuntu/acp_audit
FS="$ROOT/rootfs_amd64"
RAW="$ROOT/raw"

sudo chroot "$FS" /bin/sh -c '
set +e
printf "=== /app files outside virtual environment ===\n"
find /app -path /app/.venv -prune -o -type f -printf "%p\n" | sort
printf "=== Harbor executable and package locations (configured image PATH) ===\n"
export PATH=/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
command -v harbor
readlink -f "$(command -v harbor)"
find /app/.venv/lib/python3.12/site-packages/harbor -maxdepth 3 -type f -printf "%p\n" | sort
printf "=== Harbor CLI help ===\n"
harbor --help
printf "=== Harbor version attempt ===\n"
harbor --version
printf "harbor-version-exit=%s\n" "$?"
printf "=== Forensics and reverse-tool presence ===\n"
for tool in xxd hexdump od nm ar ldd strace ltrace gdb radare2 r2 binwalk foremost volatility vol vol.py bulk_extractor exiftool yara strings objdump readelf file tcpdump tshark wireshark capinfos editcap mergecap tcpreplay nmap nc netcat socat dig nslookup ip ss ping traceroute curl wget openssl sha256sum md5sum b3sum dd gzip xz tar zip unzip sqlite3 psql mysql redis-cli jq yq xmlstarlet python3 node go rustc cargo gcc make cmake llvm-objdump dwarfdump; do
  path=$(command -v "$tool")
  if [ "$?" -eq 0 ]; then
    printf "PRESENT\t%s\t%s\n" "$tool" "$path"
  else
    printf "ABSENT\t%s\n" "$tool"
  fi
done
' > "$RAW/harbor_forensics_probe.txt" 2>&1
