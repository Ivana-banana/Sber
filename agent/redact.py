"""Keeping our own credentials out of the trace.

The realistic leak is not us printing the key on purpose. It is the agent running
something like `env` or `cat /proc/self/environ` while exploring the machine, and the
endpoint key landing in tool output, from there in the trace, and from there in a log
we attach to an experiment record.

Redaction works on values, not on shapes: the endpoint key gets replaced wherever it
appears in text we are about to keep. Only the key: it is the one value that is ours
and secret and will never legitimately appear in task material. The base URL and model
name are our coordinates too, but they are handled by removing them from the process
environment (`tools.endpoint_scrubbed_env` and the pop in `main`), not by substring
redaction -- blanking them from every observation was over-redaction that erased a
localhost service (`curl http://127.0.0.1:8000/v1/...`) or a model-named string from
the diagnostics it was meant to protect.

What redaction never touches is the task's own environment. `POSTGRES_PASSWORD`,
`SECRET_KEY`, a CTF's signing key -- these are material the task hands the agent to
read and use, not secrets of ours to blank out of its own observations. Scrubbing them
by name marker was an active defect: `POSTGRES_PASSWORD=postgres` erased the word
`postgres` from every file the agent read, and a signed-cookie CTF whose `SECRET_KEY`
the agent had to forge with became unsolvable the moment the value went to a
placeholder.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from agent import limits

PLACEHOLDER = "<redacted>"

AGENT_CREDENTIAL_ENV_NAMES = (
    limits.API_KEY_ENV_NAME,
    limits.BASE_URL_ENV_NAME,
    limits.MODEL_ENV_NAME,
)
"""The agent's own endpoint coordinates.

These are removed from the environment of any child the agent runs, and from the
agent's own process environment once the settings are read, so that neither `env`,
`cat /proc/<pid>/environ`, nor a shell variable can reach them. This is the single
source of truth for "our endpoint variables"; `tools.endpoint_scrubbed_env` scrubs
exactly this set. Redaction (below) hides only the key's value.
"""

MIN_SECRET_LENGTH = 8
"""Short values are not redacted.

A one-character secret would turn every occurrence of that character into a
placeholder and destroy the diagnostic value of the whole trace. Nothing usable as an
API key is this short.
"""


def secrets_from_env(env: Mapping[str, str]) -> tuple[str, ...]:
    """The credential values to redact from kept text: the endpoint key, and only it.

    A tuple (rather than a bare value) so callers keep the existing `redact(text,
    secrets)` shape, and so an unset or too-short key yields an empty set that redacts
    nothing rather than a placeholder for the empty string.
    """
    value = env.get(limits.API_KEY_ENV_NAME, "").strip()
    return (value,) if len(value) >= MIN_SECRET_LENGTH else ()


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every occurrence of every secret in `text`."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, PLACEHOLDER)
    return text
