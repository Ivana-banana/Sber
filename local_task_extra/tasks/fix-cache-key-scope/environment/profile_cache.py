CACHE = {}
FETCH_LOG = []
TTL_SECONDS = 30

def clear_cache():
    CACHE.clear()
    FETCH_LOG.clear()

def _fetch_account(tenant: str, user: str) -> str:
    FETCH_LOG.append((tenant, user))
    return f"{tenant}:{user}:snapshot-{len(FETCH_LOG)}"

def render_profile(tenant: str, user: str, now: int) -> str:
    key = user
    entry = CACHE.get(key)
    if entry and now - entry["stored_at"] < TTL_SECONDS:
        return entry["payload"]
    payload = _fetch_account(tenant, user)
    CACHE[key] = {"stored_at": now, "payload": payload}
    return payload
