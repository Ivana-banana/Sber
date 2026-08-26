from pathlib import Path
p=Path('/srv/bootstrap/farewell.marker')
if not p.is_file() or p.read_bytes()!=b'Ciao': raise SystemExit(1)
