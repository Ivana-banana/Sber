"""Pure extraction of exact-file requirements from the original instruction."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.result_contract import exact_file_requirement


def test_extracts_a_quoted_path_and_utf8_content_from_an_unambiguous_instruction() -> None:
    instruction = (
        "Create a file at `/var/lib/agent output.txt` whose entire content is exactly "
        "the single word `Привет` (the file must contain only those six characters)."
    )

    assert exact_file_requirement(instruction) == (
        Path("/var/lib/agent output.txt"),
        "Привет".encode("utf-8"),
    )


def test_an_instruction_without_an_exact_content_contract_returns_none() -> None:
    instruction = "Write `/app/report.txt` with the findings and explain the result."

    assert exact_file_requirement(instruction) is None


def test_ambiguous_multiple_file_targets_return_none() -> None:
    instruction = (
        "Create a file at `/tmp/one.txt` whose content is exactly `one`, then create "
        "a file at `/tmp/two.txt` whose content is exactly `two`."
    )

    assert exact_file_requirement(instruction) is None


def test_a_non_file_exact_value_is_not_treated_as_an_exact_file_requirement() -> None:
    instruction = "The JSON report must contain exactly `findings` and preserve the source."

    assert exact_file_requirement(instruction) is None


def test_a_negated_file_creation_is_not_an_exact_file_requirement() -> None:
    instruction = "Do not create a file at `/tmp/marker.txt` whose content is exactly `x`."

    assert exact_file_requirement(instruction) is None


def test_a_content_clause_in_a_later_sentence_is_not_assumed_to_match_the_file() -> None:
    instruction = (
        "Create a file at `/tmp/report.txt` for the report. The content is exactly `x`."
    )

    assert exact_file_requirement(instruction) is None


@pytest.mark.parametrize(
    "instruction",
    [
        (
            "Create a file at `/tmp/answer.txt`; run pytest; whose entire content is "
            "exactly `x`."
        ),
        (
            "Create a file at `/tmp/answer.txt` and run pytest before making its "
            "content exactly `x`."
        ),
        (
            "Read input first. Create a file at `/tmp/answer.txt` whose content is "
            "exactly `x`."
        ),
        (
            "Create a file at `/tmp/answer.txt` whose content is exactly `x` "
            "(then inspect input)."
        ),
    ],
)
def test_an_exact_file_contract_rejects_additional_actions(instruction: str) -> None:
    assert exact_file_requirement(instruction) is None


def test_a_long_negation_is_not_treated_as_a_positive_file_contract() -> None:
    instruction = (
        "Please never, under any circumstances, create a file at `/tmp/marker.txt` "
        "whose content is exactly `x`."
    )

    assert exact_file_requirement(instruction) is None


@pytest.mark.parametrize("prefix", ["Must not", "Should not", "Shall not"])
def test_modal_negation_is_not_treated_as_a_positive_file_contract(prefix: str) -> None:
    instruction = (
        f"{prefix} create a file at `/tmp/marker.txt` whose content is exactly `x`."
    )

    assert exact_file_requirement(instruction) is None


@pytest.mark.parametrize(
    "content_clause",
    ["whose content is exactly", "with content exactly", "with its content exactly"],
)
def test_supported_content_forms_are_exact_file_contracts(content_clause: str) -> None:
    instruction = f"Create a file at `/tmp/answer.txt` {content_clause} `x`."

    assert exact_file_requirement(instruction) == (Path("/tmp/answer.txt"), b"x")


@pytest.mark.parametrize(
    "note",
    ["parse input first", "load the logs", "compare two files", "use the source"],
)
def test_an_arbitrary_parenthetical_is_not_an_exact_file_contract(note: str) -> None:
    instruction = (
        "Create a file at `/tmp/answer.txt` whose content is exactly `x` "
        f"({note})."
    )

    assert exact_file_requirement(instruction) is None


@pytest.mark.parametrize(
    "content_clause",
    ["with the content exactly", ", the content is exactly"],
)
def test_more_supported_content_forms_are_exact_file_contracts(content_clause: str) -> None:
    instruction = f"Create a file at `/tmp/answer.txt` {content_clause} `x`."

    assert exact_file_requirement(instruction) == (Path("/tmp/answer.txt"), b"x")


# --- a markdown heading in front of the sentence -----------------------------


@pytest.mark.parametrize(
    "heading",
    ["# Worker readiness beacon", "## Marker", "###### deploy marker"],
)
def test_one_leading_markdown_heading_does_not_hide_an_exact_file_contract(heading: str) -> None:
    # A heading is the title of the instruction, not a step of it, and it is the
    # dominant shape: 4 of the 6 public instructions and all 18 extra ones open with
    # one. Without this the prefix guard read the title as "something happens before
    # the write" and refused the contract, which costs the whole one-call path on a
    # class whose public tasks are given 120 s.
    instruction = (
        f"{heading}\n\nWrite a file to `/opt/runstate/ready.signal` whose entire "
        "content is exactly `Salut`."
    )

    assert exact_file_requirement(instruction) == (Path("/opt/runstate/ready.signal"), b"Salut")


def test_only_the_heading_is_skipped_not_a_sentence_of_prose() -> None:
    # The relaxation is one heading line, not "ignore what comes first". Everything
    # the prefix guard was protecting -- read this, then write that -- still refuses.
    instruction = (
        "# Beacon\n\nRead `/app/input.txt` first. Write a file to `/app/out.txt` "
        "whose entire content is exactly `Salut`."
    )

    assert exact_file_requirement(instruction) is None


def test_a_second_heading_is_not_skipped() -> None:
    instruction = (
        "# Beacon\n\n## Step one\n\nWrite a file to `/app/out.txt` whose entire "
        "content is exactly `Salut`."
    )

    assert exact_file_requirement(instruction) is None


def test_a_hash_that_is_not_a_markdown_heading_is_not_skipped() -> None:
    # `#` must open a heading (one to six of them, then a space), so a numbered note
    # or a shell comment is still prose in front of the write.
    instruction = (
        "#1 priority for the release\n\nWrite a file to `/app/out.txt` whose entire "
        "content is exactly `Salut`."
    )

    assert exact_file_requirement(instruction) is None


# --- the shapes where the answer has to be computed, not copied --------------


@pytest.mark.parametrize(
    "instruction",
    [
        # A CTF: the bytes are the thing to find, and the instruction cannot name them.
        "# Gateway challenge\n\nWrite the flag to /app/flag.txt.",
        "# Session token\n\nSave the recovered token in `/app/answer.txt`.",
        "# Beacon\n\nCreate /app/report.json with fields `status` and `checked_at`.",
        # The value is described, not quoted, so there is nothing to compare against.
        (
            "# Gateway challenge\n\nCreate a file at `/app/flag.txt` whose entire "
            "content is exactly the proof string the service exposes."
        ),
        # An investigation in front of the write is still an investigation.
        (
            "# Egress ledger\n\nRead the ledger under `/srv/case`. Create a file at "
            "`/app/answer.env` whose entire content is exactly the largest total."
        ),
        # A report: the schema is named, the bytes are not.
        (
            "# Session store review\n\nWrite `/app/audit_report.json` describing the "
            "unsafe deserialization in `/app/gateway.py`, with `category`, `location` "
            "and `evidence`."
        ),
    ],
)
def test_a_deliverable_whose_content_must_be_computed_has_no_contract(instruction: str) -> None:
    # These are the shapes the heading relaxation must not reach. `verify.verify`
    # applies this contract whatever the playbook is, so a contract invented here
    # would judge a computed flag or report against bytes copied out of the task --
    # and reject the right answer. The guards that keep it shut are the ones the
    # heading skip leaves alone: the content clause must be quoted literal bytes,
    # and nothing but the title may stand in front of the write.
    assert exact_file_requirement(instruction) is None
