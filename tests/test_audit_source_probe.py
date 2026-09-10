from __future__ import annotations

import json
from pathlib import Path

from scripts.eval import probe_audit_source as probe


def _task(tmp_path: Path) -> tuple[Path, Path]:
    task = tmp_path / "package"
    source = task / "environment" / "app" / "handler.py"
    source.parent.mkdir(parents=True)
    source.write_text("def login(name):\n    return query(name)\n", encoding="utf-8")
    (task / "instruction.md").write_text("Audit the application and write JSON.", encoding="utf-8")
    tests = task / "tests"
    tests.mkdir()
    (tests / "test_report.py").write_text(
        """import json, os
from pathlib import Path

REPORT_PATH = Path(os.environ["REPORT_PATH"])

def test_nonempty_findings():
    assert json.loads(REPORT_PATH.read_text())["findings"]
""",
        encoding="utf-8",
    )
    return task, source


def _response(content: str, *, prompt: int = 11, completion: int = 7) -> bytes:
    return json.dumps(
        {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        }
    ).encode()


def test_request_has_only_instruction_selected_source_and_schema(tmp_path: Path) -> None:
    task, source = _task(tmp_path)
    seen: list[dict] = []
    answer = json.dumps({"findings": [{
        "title": "Unsafe query", "severity": "high", "category": "Injection",
        "location": "app/handler.py", "evidence": "query(name)",
        "impact": "Data access", "recommendation": "Bind parameters",
    }]})

    def transport(url: str, body: bytes, headers: dict[str, str], timeout: float):
        seen.append(json.loads(body))
        return 200, _response(answer)

    result = probe.run_probe(
        task=task, source=source, out=tmp_path / "out", model="stub",
        base_url="http://stub/v1", api_key="secret-canary", transport=transport,
    )

    request = json.dumps(seen, ensure_ascii=False).lower()
    assert "audit the application" in request
    assert "app/handler.py" in request
    assert "return query(name)" in request
    assert "solution" not in request
    assert "checker" not in request
    assert "expected" not in request
    assert "secret-canary" not in json.dumps(result)
    assert result["metadata"]["assisted"] is True
    assert result["metadata"]["eligible_for_public_score"] is False
    assert result["usage"] == {"input_tokens": 11, "output_tokens": 7, "reported": True}
    assert result["probe_success"] is True
    assert result["assertions"] == [{"name": "test_nonempty_findings", "outcome": "passed"}]


def test_invalid_json_and_empty_findings_cannot_succeed(tmp_path: Path) -> None:
    for index, content in enumerate(("not json", '{"findings": []}')):
        task, source = _task(tmp_path / str(index))

        def transport(url: str, body: bytes, headers: dict[str, str], timeout: float):
            return 200, _response(content)

        result = probe.run_probe(
            task=task, source=source, out=tmp_path / f"out-{index}", model="stub",
            base_url="http://stub/v1", api_key="", transport=transport,
        )
        assert result["probe_success"] is False
        assert result["failure_kind"] == "content"
        assert result["checker_exit"] != 0
        assert (tmp_path / f"out-{index}" / "raw_response.txt").read_text() == content


def test_endpoint_error_is_distinct_from_bad_content(tmp_path: Path) -> None:
    task, source = _task(tmp_path)

    def transport(url: str, body: bytes, headers: dict[str, str], timeout: float):
        raise OSError("stub is down")

    result = probe.run_probe(
        task=task, source=source, out=tmp_path / "out", model="stub",
        base_url="http://stub/v1", api_key="", transport=transport,
        sleep=lambda _: None,
    )

    assert result["probe_success"] is False
    assert result["failure_kind"] == "endpoint"
    assert result["endpoint_error"].startswith("transport failed:")
    assert result["checker_exit"] != 0


def test_reused_output_directory_is_rejected(tmp_path: Path) -> None:
    task, source = _task(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "old").write_text("keep", encoding="utf-8")

    try:
        probe.run_probe(
            task=task, source=source, out=out, model="stub",
            base_url="http://stub/v1", api_key="",
        )
    except FileExistsError:
        pass
    else:
        raise AssertionError("reused output was accepted")


def test_the_action_envelope_is_unwrapped_before_the_checker_sees_it(tmp_path: Path) -> None:
    """The checker reads the report, not the action that carried it."""
    task, source = _task(tmp_path)
    report = json.dumps({"findings": [{"title": "Unsafe query"}]})
    seen: list[dict] = []

    def transport(url: str, body: bytes, headers: dict[str, str], timeout: float):
        seen.append(json.loads(body))
        return 200, _response(json.dumps({"tool": "write_file", "path": "/app/r.json",
                                          "content": report}))

    result = probe.run_probe(
        task=task, source=source, out=tmp_path / "out", model="stub",
        base_url="http://stub/v1", api_key="", as_action=True, transport=transport,
    )

    assert result["metadata"]["envelope"] == "action"
    assert (tmp_path / "out" / "candidate.json").read_text() == report
    assert result["probe_success"] is True
    assert "write_file" in json.dumps(seen)


def test_an_action_that_writes_nothing_keeps_its_own_text(tmp_path: Path) -> None:
    task, source = _task(tmp_path)

    def transport(url: str, body: bytes, headers: dict[str, str], timeout: float):
        return 200, _response(json.dumps({"tool": "list_dir", "path": "/app"}))

    result = probe.run_probe(
        task=task, source=source, out=tmp_path / "out", model="stub",
        base_url="http://stub/v1", api_key="", as_action=True, transport=transport,
    )

    assert result["probe_success"] is False
    assert "list_dir" in (tmp_path / "out" / "candidate.json").read_text()
