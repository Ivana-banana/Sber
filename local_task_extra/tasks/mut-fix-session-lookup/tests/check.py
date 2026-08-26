import importlib.util,tempfile
from pathlib import Path
spec=importlib.util.spec_from_file_location('s','/app/portal/session_store.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as d:
 p=d+'/sessions.db'; m.seed(p)
 if not m.verify(p,'mira','orchid-41'): raise SystemExit(1)
 if m.verify(p,"mira' OR '1'='1' -- ",'x'): raise SystemExit(1)
s=Path('/app/portal/session_store.py').read_text()
if 'f"SELECT' in s or "f'SELECT" in s: raise SystemExit(1)
