import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('redirector', '/app/redirector.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
valid = 'https://console.internal.example/team?a=1'
if m.safe_redirect(valid) != valid: raise SystemExit(1)
for bad in ('http://console.internal.example/x', 'https://console.internal.example.evil/x', 'https://console.internal.example@evil/x', '//console.internal.example/x', 'https://console.internal.example:444/x'):
    if m.safe_redirect(bad) != m.DEFAULT_URL: raise SystemExit(1)
s = Path('/app/redirector.py').read_text()
if '.startswith(ALLOWED_ORIGIN)' in s: raise SystemExit(1)
