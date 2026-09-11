"""Pure extraction of exact-file requirements from the original instruction."""

from __future__ import annotations

from pathlib import Path

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
