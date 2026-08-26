#!/bin/bash
set -euo pipefail
python3 /app/server.py > /app/server.log 2>&1 &
exec tail -f /dev/null
