#!/usr/bin/env bash
set -euo pipefail
ROOT=/home/ubuntu/acp_audit
FS="$ROOT/rootfs_amd64"
RAW="$ROOT/raw"
sudo chroot "$FS" /bin/sh -c '
set +e
printf "=== base interpreter: python -m pip list ===\n"
/usr/local/bin/python3.12 -m pip list
printf "=== base interpreter: python -m pip freeze ===\n"
/usr/local/bin/python3.12 -m pip freeze
printf "=== venv interpreter: python -m pip list ===\n"
/app/.venv/bin/python -m pip list
printf "venv-python-pip-list-exit=%s\n" "$?"
printf "=== venv interpreter: python -m pip freeze ===\n"
/app/.venv/bin/python -m pip freeze
printf "venv-python-pip-freeze-exit=%s\n" "$?"
printf "=== base pip, explicit venv site-packages: pip list --path ===\n"
/usr/local/bin/python3.12 -m pip list --path /app/.venv/lib/python3.12/site-packages
printf "venv-path-pip-list-exit=%s\n" "$?"
printf "=== base pip, explicit venv site-packages: pip freeze --path ===\n"
/usr/local/bin/python3.12 -m pip freeze --path /app/.venv/lib/python3.12/site-packages
printf "venv-path-pip-freeze-exit=%s\n" "$?"
' > "$RAW/pip_full_inventory.txt" 2>&1
