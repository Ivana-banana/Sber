from pathlib import Path
exp={'attacker_ip':'198.51.100.86','compromised_user':'guest-upload','webshell_path':'/uploads/shell.php','exfil_bytes':'7340032'}
p=Path('/app/incident_report.txt')
try: lines=p.read_text().splitlines(); got=dict(x.split('=',1) for x in lines)
except Exception: raise SystemExit(1)
if len(lines)!=4 or got!=exp or any(not x or x.count('=')!=1 for x in lines): raise SystemExit(1)
