#!/bin/bash
set -euo pipefail
cat > /app/unpack.py <<'PY'
from pathlib import Path
from zipfile import ZipFile

def unpack(upload: str, destination: str) -> None:
    base = Path(destination).resolve()
    base.mkdir(parents=True, exist_ok=True)
    with ZipFile(upload) as archive:
        for member in archive.infolist():
            target = (base / member.filename).resolve()
            try:
                target.relative_to(base)
            except ValueError:
                raise ValueError("archive member escapes destination")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as src, target.open("wb") as dst:
                    dst.write(src.read())
PY
