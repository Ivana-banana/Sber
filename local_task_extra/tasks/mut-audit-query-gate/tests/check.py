import json
from pathlib import Path
p=Path('/workspace/review/findings.json')
try: fs=json.loads(p.read_text())['observations']
except Exception: raise SystemExit(1)
if not isinstance(fs,list) or len(fs)!=1 or not isinstance(fs[0],dict): raise SystemExit(1)
f=fs[0]
if set(f)!= {'kind','component','proof','remedy'}: raise SystemExit(1)
if f['kind']!='sql_injection' or f['component']!='portal/query_gate.py:authorize': raise SystemExit(1)
if 'login_name' not in f['proof'] or not f['remedy'].strip(): raise SystemExit(1)
