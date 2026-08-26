#!/bin/bash
set -euo pipefail
python3 - <<'PY'
p='/app/profile_cache.py'
s=open(p).read()
s=s.replace('    key = user\n', '    key = (tenant, user)\n')
open(p,'w').write(s)
PY
