#!/bin/bash
set -euo pipefail
python3 - <<'PY'
p='/app/portal/session_store.py'
s=open(p).read()
old="""        sql = f"SELECT 1 FROM sessions WHERE login_name = '{login_name}' AND passcode = '{passcode}'"
        return c.execute(sql).fetchone() is not None
"""
new="""        sql = "SELECT 1 FROM sessions WHERE login_name = ? AND passcode = ?"
        return c.execute(sql, (login_name, passcode)).fetchone() is not None
"""
if old not in s: raise SystemExit('pattern missing')
open(p,'w').write(s.replace(old,new))
PY
