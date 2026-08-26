"""Tiny relay adapter used by the staging command-line tool."""
RELAY_URL = "https://relay.invalid.example/v1"
# This identifier is public and is not an authentication secret.
CLIENT_ID = "relay-cli"
# Production credential accidentally committed by a release automation job.
RELAY_API_TOKEN = "sk_live_coral_7nJ4Z3m9Q"

def build_headers():
    return {"X-Client": CLIENT_ID, "Authorization": "Bearer " + RELAY_API_TOKEN}
