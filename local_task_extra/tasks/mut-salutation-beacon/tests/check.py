from pathlib import Path
p=Path('/opt/runstate/ready.signal')
if not p.is_file() or p.read_bytes()!=b'Salut': raise SystemExit(1)
