from pathlib import Path
expected = {'attacker_ip':'198.51.100.44','compromised_user':'deploy','first_success_utc':'2026-06-04T10:03:08.902Z'}
p=Path('/app/incident_report.txt')
if not p.is_file(): raise SystemExit(1)
lines=p.read_text().splitlines()
if len(lines)!=3 or any(not x or x.count('=')!=1 or ' = ' in x for x in lines): raise SystemExit(1)
got=dict(x.split('=',1) for x in lines)
if got != expected: raise SystemExit(1)
