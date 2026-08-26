import importlib.util
import tempfile
import zipfile
from pathlib import Path
spec = importlib.util.spec_from_file_location('unpack', '/app/unpack.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as td:
    root = Path(td); good = root/'good.zip'; dest = root/'out'
    with zipfile.ZipFile(good, 'w') as z: z.writestr('docs/readme.txt', 'ok')
    m.unpack(str(good), str(dest))
    if (dest/'docs/readme.txt').read_text() != 'ok': raise SystemExit(1)
    bad = root/'bad.zip'; outside = root/'escaped.txt'
    with zipfile.ZipFile(bad, 'w') as z: z.writestr('../escaped.txt', 'bad')
    try: m.unpack(str(bad), str(dest))
    except ValueError: pass
    else: raise SystemExit(1)
    if outside.exists(): raise SystemExit(1)
if 'extractall' in Path('/app/unpack.py').read_text(): raise SystemExit(1)
