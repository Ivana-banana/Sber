"""The leak we are guarding against is the agent dumping our own endpoint key.

Redaction scrubs only the key's value from kept text. The base URL and model are our
coordinates too, but they are removed from the environment (see tools/main), not
substring-redacted -- blanking them from every observation erased localhost services
and model-named strings from the diagnostics. The task's own environment is material
the agent must read and use, so it is never redacted.
"""

from __future__ import annotations

from agent import limits
from agent.redact import PLACEHOLDER, redact, secrets_from_env


def test_only_the_endpoint_key_value_is_collected() -> None:
    env = {
        limits.API_KEY_ENV_NAME: "sk-0123456789abcdef",
        limits.BASE_URL_ENV_NAME: "http://127.0.0.1:8000/v1",
        limits.MODEL_ENV_NAME: "qwen2.5-coder-7b",
        # The task's own environment -- read-and-use material, never our secret to hide.
        "GITHUB_TOKEN": "ghp_0123456789abcdef",
        "DB_PASSWORD": "hunter2hunter2",
        "SECRET_KEY": "signing-key-a-ctf-must-forge-with",
        "POSTGRES_PASSWORD": "postgres",
        "PATH": "/usr/bin:/bin",
    }
    assert set(secrets_from_env(env)) == {"sk-0123456789abcdef"}


def test_the_base_url_and_model_are_not_substring_redacted() -> None:
    # F8 was reverted: redacting these from every observation blanked a localhost
    # service (curl http://127.0.0.1:8000/v1/...) and any model-named string. They are
    # kept out of reach by removing them from the environment, not by redaction.
    env = {
        limits.API_KEY_ENV_NAME: "sk-0123456789abcdef",
        limits.BASE_URL_ENV_NAME: "http://127.0.0.1:8000/v1",
        limits.MODEL_ENV_NAME: "qwen2.5-coder-7b",
    }
    observation = "curl http://127.0.0.1:8000/v1/login served by qwen2.5-coder-7b"
    assert redact(observation, secrets_from_env(env)) == observation


def test_the_tasks_own_secrets_are_not_redacted_from_observations() -> None:
    # T2: a name-marker scan turned POSTGRES_PASSWORD=postgres into a blanket erasure of
    # the word "postgres" from every observation, and hid the very key a signed-cookie
    # CTF has to read. The task environment is not ours to redact.
    env = {"POSTGRES_PASSWORD": "postgres", "SECRET_KEY": "flag-signing-secret-value"}
    observation = "connect as postgres; the cookie is signed with flag-signing-secret-value"
    assert redact(observation, secrets_from_env(env)) == observation


def test_short_keys_are_left_alone() -> None:
    # Redacting "x" would replace every x in the trace and destroy its usefulness.
    assert secrets_from_env({limits.API_KEY_ENV_NAME: "x"}) == ()


def test_a_leaked_environment_dump_scrubs_the_key_and_keeps_the_rest() -> None:
    env = {
        limits.API_KEY_ENV_NAME: "sk-0123456789abcdef",
        limits.BASE_URL_ENV_NAME: "http://10.0.0.5:8000/v1",
    }
    dump = (
        f"{limits.BASE_URL_ENV_NAME}=http://10.0.0.5:8000/v1\n"
        f"{limits.API_KEY_ENV_NAME}=sk-0123456789abcdef\nHOME=/root"
    )
    scrubbed = redact(dump, secrets_from_env(env))

    assert "sk-0123456789abcdef" not in scrubbed, "the key is gone"
    assert "HOME=/root" in scrubbed, "unrelated diagnostics survive redaction"


def test_redact_replaces_every_provided_secret() -> None:
    assert redact("a=A1 b=B2", ["A1", "B2"]) == f"a={PLACEHOLDER} b={PLACEHOLDER}"


def test_redacting_without_secrets_changes_nothing() -> None:
    assert redact("plain text", ()) == "plain text"
