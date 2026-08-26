import json
from pathlib import Path

p = Path('/app/audit_report.json')
if not p.is_file():
    raise SystemExit(1)
try:
    data = json.loads(p.read_text(encoding='utf-8'))
except Exception:
    raise SystemExit(1)
findings = data.get('findings') if isinstance(data, dict) else None
if not isinstance(findings, list) or len(findings) != 1 or not isinstance(findings[0], dict):
    raise SystemExit(1)
f = findings[0]
required = {'category', 'location', 'evidence', 'severity', 'recommendation'}
if set(f) != required or f['category'] != 'hardcoded_secret':
    raise SystemExit(1)
if f['location'] != 'client.py:6' or 'sk_live_coral_7nJ4Z3m9Q' not in f['evidence']:
    raise SystemExit(1)
if f['severity'].lower() not in {'high', 'critical'} or not f['recommendation'].strip():
    raise SystemExit(1)
