from pathlib import Path
exp={'attacker_ip':'203.0.113.204','compromised_user':'finance-analyst','dataset':'ledger-2025','exfil_bytes':'18874368','first_malicious_event_utc':'2026-06-20T03:12:05.444Z'}
p=Path('/app/incident_report.txt')
try: lines=p.read_text().splitlines(); got=dict(x.split('=',1) for x in lines)
except Exception: raise SystemExit(1)
if len(lines)!=5 or got!=exp or any(not x or x.count('=')!=1 for x in lines): raise SystemExit(1)
