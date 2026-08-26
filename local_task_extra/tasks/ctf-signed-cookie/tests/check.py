from pathlib import Path
flag='FLAG{cookie_math_81d2}'
if not Path('/app/flag.txt').is_file() or Path('/app/flag.txt').read_bytes()!=flag.encode(): raise SystemExit(1)
if Path('/app/issued.flag').read_text()!=flag: raise SystemExit(1)
