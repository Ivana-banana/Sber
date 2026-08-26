# Console redirect helper

Fix `/app/redirector.py` so `safe_redirect(next_url)` returns a requested URL only when it belongs to the exact HTTPS origin `https://console.internal.example`. Otherwise it must return `DEFAULT_URL`. Paths and query strings on the valid origin are allowed. Do not add dependencies or rename the function/constants.

The verifier includes origin-confusion and parser-disagreement cases; a string-prefix check is insufficient.
