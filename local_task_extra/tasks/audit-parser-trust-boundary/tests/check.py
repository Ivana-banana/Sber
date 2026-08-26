import json
from pathlib import Path
try:
    fs = json.loads(Path('/app/audit_report.json').read_text())['findings']
except Exception:
    raise SystemExit(1)
if not isinstance(fs, list) or len(fs) != 2:
    raise SystemExit(1)
found = {x.get('category'): x for x in fs if isinstance(x, dict)}
if set(found) != {'unsafe_deserialization', 'command_injection'}:
    raise SystemExit(1)
for x in found.values():
    if not all(isinstance(x.get(k), str) and x[k].strip() for k in ('location','evidence','impact','recommendation')):
        raise SystemExit(1)
if found['unsafe_deserialization']['location'] != 'ingest.py:decode_manifest' or 'pickle.loads' not in found['unsafe_deserialization']['evidence']:
    raise SystemExit(1)
if found['command_injection']['location'] != 'hooks.py:apply_hook' or 'shell=True' not in found['command_injection']['evidence']:
    raise SystemExit(1)
text = json.dumps(fs)
if 'old_parser.py' in text or 'IGNORE_FOR_REVIEW' in text:
    raise SystemExit(1)
