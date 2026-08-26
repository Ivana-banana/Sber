#!/usr/bin/env python3
"""Проверяет исключительно безвредные marker-файлы тестовых фикстур."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--root', type=Path, default=Path(__file__).parent)
args = parser.parse_args()
root = args.root.resolve()
manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
failed = []
for case in manifest['cases']:
    marker = root / case['marker']
    if marker.exists():
        failed.append({'id': case['id'], 'marker': case['marker'], 'criterion': case['failure']})
print(json.dumps({'passed': not failed, 'failed_cases': failed}, ensure_ascii=False, indent=2))
raise SystemExit(1 if failed else 0)
