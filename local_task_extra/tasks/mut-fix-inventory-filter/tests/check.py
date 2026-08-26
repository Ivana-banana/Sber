import importlib.util,tempfile
from pathlib import Path
spec=importlib.util.spec_from_file_location('f','/app/catalog/filter.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as d:
 p=d+'/stock.db';m.seed(p)
 if m.match_names(p,'cable') != ['amber cable','cobalt cable']: raise SystemExit(1)
 if m.match_names(p,"' OR 1=1 -- ") != []: raise SystemExit(1)
s=Path('/app/catalog/filter.py').read_text()
if 'f"SELECT' in s or "f'SELECT" in s: raise SystemExit(1)
