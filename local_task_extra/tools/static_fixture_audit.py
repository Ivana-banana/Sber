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
if not root.is_dir():
    raise SystemExit(f'no tasks directory at {root}')
actual = {p.name for p in root.iterdir() if p.is_dir()}
assert actual == expected, f'package set drifted: unexpected {sorted(actual - expected)}, missing {sorted(expected - actual)}'
errors = []
for name in sorted(expected):
    base = root / name
    # A package that lost a file is the whole reason this audit exists, so the checks
    # below must report it instead of dying on it: `stat()` and `read_text()` on a
    # missing path used to raise outside the try and abandon the remaining packages,
    # with the errors collected so far never printed.
    missing_files = [
        rel
        for rel in ('task.toml', 'instruction.md', 'environment/Dockerfile', 'tests/test.sh', 'solution/solve.sh')
        if not (base / rel).is_file()
    ]
    for rel in missing_files:
        errors.append(f'{name}: missing {rel}')
    if missing_files:
        continue
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
        # The envelope of THIS set, not of the schema: the public packages legitimately
        # declare cpus=2, memory_mb=4096. 2048 was chosen deliberately in f418d3a ("put
        # the fixtures inside the envelope the real packages declare"); the audit kept
        # asserting the 256 of the original Manus delivery and had been failing every
        # package since. If the envelope moves again, it moves here and in the manifests
        # together — that is what this line is for.
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
