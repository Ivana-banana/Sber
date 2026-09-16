from __future__ import annotations

import os
import re
import stat
import sys
import tomllib
from pathlib import Path

root = Path(__file__).resolve().parents[1] / 'tasks'
expected = {
    'audit-hardcoded-token', 'audit-path-normalization', 'audit-parser-trust-boundary',
    'fix-zip-slip-extract', 'fix-redirect-scheme', 'fix-cache-key-scope',
    'forensics-ssh-bruteforce', 'forensics-webshell-chain', 'forensics-sharded-exfil',
    'ctf-header-override', 'ctf-signed-cookie', 'ctf-duplicate-parameter',
    'mut-greeting-farewell', 'mut-salutation-beacon', 'mut-audit-query-gate',
    'mut-fix-session-lookup', 'mut-fix-inventory-filter', 'mut-forensics-egress-ledger',
}
actual = {p.name for p in root.iterdir() if p.is_dir()}
assert actual == expected, (actual ^ expected)
errors = []
for name in sorted(expected):
    base = root / name
    for rel in ('task.toml', 'instruction.md', 'environment/Dockerfile', 'tests/test.sh', 'solution/solve.sh'):
        if not (base / rel).is_file():
            errors.append(f'{name}: missing {rel}')
    for rel in ('tests/test.sh', 'solution/solve.sh'):
        if not ((base / rel).stat().st_mode & stat.S_IXUSR):
            errors.append(f'{name}: {rel} is not executable')
    try:
        doc = tomllib.loads((base / 'task.toml').read_text())
        # Every assertion carries its own message. A bare `assert` here reported
        # `invalid manifest: ` with an empty reason for all 18 packages at once, which
        # is why the stale memory figure below went unnoticed for days.
        assert doc['schema_version'] == '1.2', f"schema_version {doc.get('schema_version')!r}"
        assert doc['task']['name'] == f'local/{name}', f"task.name {doc['task']['name']!r}"
        assert doc['environment']['allow_internet'] is False, 'allow_internet is not False'
        assert doc['environment']['cpus'] == 1, f"cpus {doc['environment']['cpus']}"
        # 2048 is what all 18 packages declare; the audit pinned 256 from an earlier
        # generation of the fixtures and had been failing every package since.
        assert doc['environment']['memory_mb'] == 2048, f"memory_mb {doc['environment']['memory_mb']}"
        missing = set(('task','metadata','verifier','agent','environment','solution')) - set(doc)
        assert not missing, f'missing sections {sorted(missing)}'
    except Exception as e:
        errors.append(f'{name}: invalid manifest: {e}')
    shell = (base / 'tests/test.sh').read_text()
    if '/logs/verifier/reward.txt' not in shell or "printf '1" not in shell or "printf '0" not in shell:
        errors.append(f'{name}: verifier is not explicitly binary')
    payload = '\n'.join(p.read_text(errors='ignore') for p in (base / 'environment').rglob('*') if p.is_file())
    forbidden = ('apt-get ', 'pip install', 'uv pip', 'curl ', 'wget ', 'requests.get(', 'socket.create_connection')
    found = [x for x in forbidden if x in payload]
    if found:
        errors.append(f'{name}: prohibited external/dependency token {found}')

if errors:
    print('\n'.join(errors), file=sys.stderr)
    raise SystemExit(1)
print(f'STATIC_PASS packages={len(expected)} offline_manifests={len(expected)}')
