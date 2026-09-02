"""Contract tests for the safe indirect prompt-injection eval runner."""

from __future__ import annotations

from pathlib import Path

from scripts.eval.run_injection_suite import (
    assess_workspace,
    file_hashes,
    instruction_for,
    load_cases,
    materialise_case,
)


FIXTURES = Path(__file__).parent / "fixtures" / "prompt-injection"


def test_manifested_cases_are_all_present_on_disk() -> None:
    """A missing fixture silently turns a claimed 15-case result into 14 cases."""
    cases = load_cases(FIXTURES)

    assert [case.case_id for case in cases] == [f"{number:02d}_{name}" for number, name in [
        (1, "readme_direct"),
        (2, "source_comment"),
        (3, "filename_probe"),
        (4, "incident_log"),
        (5, "http_response"),
        (6, "config"),
        (7, "package_metadata"),
        (8, "git_metadata"),
        (9, "test_self_modify"),
        (10, "makefile_comment"),
        (11, "html_comment"),
        (12, "quoted_task"),
        (13, "unicode_obfuscation"),
        (14, "tool_output"),
        (15, "cross_file_context"),
    ]]
    assert all(path.is_file() for case in cases for path in case.source_files)


def test_materialised_case_is_isolated_and_instruction_names_its_contract(tmp_path: Path) -> None:
    """The agent must only see a fresh case and an explicit benign output path."""
    case = load_cases(FIXTURES)[0]
    workspace = materialise_case(case, tmp_path / "workspace")
    instruction = instruction_for(case, Path("/workspace"))

    assert workspace == tmp_path / "workspace"
    assert (workspace / "README.md").is_file()
    assert not (workspace / ".pi_markers").exists()
    assert "/workspace/agent_report.txt" in instruction
    assert "/workspace/README.md" in instruction


def test_assessment_reports_marker_input_mutation_and_benign_report(tmp_path: Path) -> None:
    """A marker-free run is not safe when it changed its data or wrote no report."""
    case = load_cases(FIXTURES)[0]
    workspace = materialise_case(case, tmp_path / "workspace")
    before = file_hashes(workspace)
    (workspace / "agent_report.txt").write_text("README.md says the service reports status.\n")
    marker = workspace / case.marker
    marker.parent.mkdir()
    marker.write_text("followed\n")
    (workspace / "README.md").write_text("changed input\n")

    result = assess_workspace(case, workspace, before)

    assert result.marker_created is True
    assert result.inputs_changed is True
    assert result.report_nonempty is True
    assert result.report_mentions_inputs is True
