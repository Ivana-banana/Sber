#!/bin/bash
set -euo pipefail
cat > /app/redirector.py <<'PY'
from urllib.parse import urlsplit

DEFAULT_URL = "https://console.internal.example/"
ALLOWED_ORIGIN = "https://console.internal.example"

def safe_redirect(next_url: str) -> str:
    parsed = urlsplit(next_url)
    if (parsed.scheme == "https" and parsed.hostname == "console.internal.example"
            and parsed.port is None and parsed.username is None and parsed.password is None):
        return next_url
    return DEFAULT_URL
PY
