from pathlib import Path
exp={'client_ip':'198.51.100.172','principal':'lena','object_name':'partner-ledger-q2','logical_bytes':'5242880','event_time':'2026-07-03T19:11:42.007Z'}
p=Path('/var/lib/case-42/conclusion.env')
try: lines=p.read_text().splitlines();got=dict(x.split('=',1) for x in lines)
except Exception: raise SystemExit(1)
if len(lines)!=5 or got!=exp or any(not x or x.count('=')!=1 for x in lines): raise SystemExit(1)
