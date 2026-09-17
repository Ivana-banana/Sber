import json
from pathlib import Path
V_LOGIN = ("        query = (\n            f\"SELECT id FROM users \"\n            f\"WHERE username = '{req.username}' AND password = '{req.password}'\"\n        )\n        row = await conn.fetchrow(query)",
           "        row = await conn.fetchrow(\n            \"SELECT id FROM users WHERE username = $1 AND password = $2\",\n            req.username,\n            req.password,\n        )")
V_SEARCH = ("        # VULNERABLE: f-string LIKE concatenation\n        query = f\"SELECT * FROM items WHERE name LIKE '%{q}%' ORDER BY id ASC\"\n        rows = await conn.fetch(query)",
            "        rows = await conn.fetch(\n            \"SELECT * FROM items WHERE name LIKE $1 ORDER BY id ASC\",\n            f\"%{q}%\",\n        )")
def script(path, old_new):
    o, n = old_new
    return [
        {"tool": "run_shell", "command": "sleep 25 && ps aux | grep -i uvicorn | head -5"},
        {"tool": "read_range", "path": path, "start_line": 1},
        {"tool": "replace_text", "path": path, "old": o, "new": n},
        {"tool": "finish", "path": path, "verify": "cd /app && python3 -m pytest -q tests/ 2>&1 | tail -5", "summary": "parametrized"},
        {"tool": "run_shell", "command": "kill $KILL"},
        {"tool": "run_shell", "command": "$READY"},
        {"tool": "run_shell", "command": "sleep 3 && curl -s http://127.0.0.1:8000/healthz"},
        {"tool": "run_shell", "command": "cd /app && python3 -m pytest -q tests/ 2>&1 | tail -5"},
        {"tool": "finish", "path": path, "verify": "cd /app && python3 -m pytest -q tests/", "summary": "parametrized"},
    ]
import sys
which = sys.argv[1]
S = {"Run `pytest tests/`": script("/app/routers/auth.py", V_LOGIN) if which == "login" else script("/app/routers/items.py", V_SEARCH)}
json.dump(S, open(Path(__file__).with_name(f"public-{which}.json"), "w"), ensure_ascii=False, indent=1)
