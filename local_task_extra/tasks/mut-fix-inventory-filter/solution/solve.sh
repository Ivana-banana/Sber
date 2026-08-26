#!/bin/bash
set -euo pipefail
python3 - <<'PY'
p='/app/catalog/filter.py';s=open(p).read()
old="""        query = f"SELECT name FROM stock WHERE name LIKE '%{phrase}%' ORDER BY name"
        return [row[0] for row in c.execute(query)]
"""
new="""        query = "SELECT name FROM stock WHERE name LIKE ? ORDER BY name"
        return [row[0] for row in c.execute(query, (f"%{phrase}%",))]
"""
if old not in s: raise SystemExit('pattern missing')
open(p,'w').write(s.replace(old,new))
PY
