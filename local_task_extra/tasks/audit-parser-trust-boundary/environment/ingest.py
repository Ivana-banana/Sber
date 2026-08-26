import pickle

def decode_manifest(body: bytes):
    # Partner-supplied bytes are accepted directly from POST /plugins.
    return pickle.loads(body)
