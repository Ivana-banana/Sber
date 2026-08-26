from pathlib import Path
import os

EXPORT_ROOT = Path('/srv/exports')

def open_export(account: str, requested_name: str) -> bytes:
    # requested_name comes from the query parameter named file.
    candidate = EXPORT_ROOT / account / requested_name
    return candidate.read_bytes()

def publish_artifact(account: str, upload_name: str, content: bytes) -> Path:
    account_dir = EXPORT_ROOT / account
    account_dir.mkdir(parents=True, exist_ok=True)
    destination = account_dir / upload_name
    destination.write_bytes(content)
    return destination

def read_latest(account: str) -> bytes:
    # latest is created by a privileged batch job and may be a symlink.
    return (EXPORT_ROOT / account / 'latest').read_bytes()
