from pathlib import Path
flag='FLAG{hpp_split_brain_44af}'
p=Path('/app/flag.txt')
if not p.is_file() or p.read_bytes()!=flag.encode(): raise SystemExit(1)
if Path('/app/issued.flag').read_text()!=flag: raise SystemExit(1)
