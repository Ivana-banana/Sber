DEFAULT_URL = "https://console.internal.example/"
ALLOWED_ORIGIN = "https://console.internal.example"

def safe_redirect(next_url: str) -> str:
    if next_url.startswith(ALLOWED_ORIGIN):
        return next_url
    return DEFAULT_URL
