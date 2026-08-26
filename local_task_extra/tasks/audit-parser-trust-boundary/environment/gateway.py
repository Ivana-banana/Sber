from ingest import decode_manifest
from hooks import apply_hook

def handle_upload(body: bytes, hook_name: str):
    manifest = decode_manifest(body)
    return apply_hook(hook_name, manifest)
