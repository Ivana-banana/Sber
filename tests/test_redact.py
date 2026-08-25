"""The leak we are guarding against is the agent dumping the environment itself."""

from __future__ import annotations

from agent.redact import PLACEHOLDER, redact, secrets_from_env


def test_credential_shaped_names_are_collected() -> None:
    env = {
        "OPENAI_API_KEY": "sk-0123456789abcdef",
        "GITHUB_TOKEN": "ghp_0123456789abcdef",
        "DB_PASSWORD": "hunter2hunter2",
        "MY_SECRET": "topsecretvalue",
        "PATH": "/usr/bin:/bin",
        "LOCAL_AGENT_MODEL": "small-local",
    }
    assert set(secrets_from_env(env)) == {
        "sk-0123456789abcdef",
        "ghp_0123456789abcdef",
        "hunter2hunter2",
        "topsecretvalue",
    }


def test_short_values_are_left_alone() -> None:
    # Redacting "x" would replace every x in the trace and destroy its usefulness.
    assert secrets_from_env({"API_KEY": "x"}) == ()


def test_longest_first_so_a_prefix_does_not_mangle_a_longer_secret() -> None:
    env = {"A_KEY": "abcdefgh", "B_KEY": "abcdefgh-extended-value"}
    secrets = secrets_from_env(env)
    dumped = "token=abcdefgh-extended-value"
    assert redact(dumped, secrets) == f"token={PLACEHOLDER}"


def test_a_leaked_environment_dump_is_scrubbed() -> None:
    env = {"OPENAI_API_KEY": "sk-0123456789abcdef"}
    dump = "OPENAI_BASE_URL=http://x/v1\nOPENAI_API_KEY=sk-0123456789abcdef\nHOME=/root"
    scrubbed = redact(dump, secrets_from_env(env))

    assert "sk-0123456789abcdef" not in scrubbed
    assert "OPENAI_BASE_URL=http://x/v1" in scrubbed, "diagnostics must survive redaction"


def test_redacting_without_secrets_changes_nothing() -> None:
    assert redact("plain text", ()) == "plain text"
