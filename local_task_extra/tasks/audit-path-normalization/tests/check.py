import json
from pathlib import Path
p = Path('/app/audit_report.json')
try:
    report = json.loads(p.read_text())
    fs = report['findings']
except Exception:
    raise SystemExit(1)
if not isinstance(fs, list) or len(fs) != 2:
    raise SystemExit(1)
by_category = {f.get('category'): f for f in fs if isinstance(f, dict)}
if set(by_category) != {'path_traversal', 'symlink_escape'}:
    raise SystemExit(1)
tr = by_category['path_traversal']
sy = by_category['symlink_escape']
for finding in (tr, sy):
    if not all(isinstance(finding.get(k), str) and finding[k].strip() for k in ('location','evidence','impact','recommendation')):
        raise SystemExit(1)
if tr['location'] != 'export_store.py:open_export' or 'requested_name' not in tr['evidence']:
    raise SystemExit(1)
if sy['location'] != 'export_store.py:read_latest' or 'symlink' not in sy['evidence'].lower():
    raise SystemExit(1)
