"""Tests for codex_jobs/findings.py, run as a script.

Covers verdict normalization, ID assignment, severity ordering, body synthesis,
confidence coercion, extra-key stashing, and error paths to ensure the JSON
validator handles real-world LLM output robustly.
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "codex_jobs" / "findings.py"
PYTHON = sys.executable


def run_validator(payload: dict, tmp_path: Path):
    """Write payload as JSON to a temp file and run the validator."""
    json_path = tmp_path / "round-1-findings.json"
    json_path.write_text(json.dumps(payload))
    result = subprocess.run([PYTHON, str(SCRIPT), str(json_path)], capture_output=True, text=True)
    return result, json_path


def minimal_finding(**overrides) -> dict:
    """Return a minimal valid finding dict, with optional field overrides."""
    base = {
        "severity": "high",
        "title": "Missing auth check",
        "body": "The endpoint has no authentication guard.",
        "recommendation": "Add CurrentUser dependency.",
    }
    base.update(overrides)
    return base


def minimal_payload(**overrides) -> dict:
    """Return a minimal valid top-level payload, with optional overrides."""
    base = {
        "verdict": "needs-attention",
        "summary": "One high severity finding.",
        "findings": [minimal_finding()],
    }
    base.update(overrides)
    return base


def test_a_valid_file_gets_ids_and_a_markdown_rendering(tmp_path):
    payload = minimal_payload(
        findings=[
            minimal_finding(
                severity="critical",
                title="SQL injection",
                body="Raw query.",
                recommendation="Parameterize.",
            ),
            minimal_finding(
                severity="low", title="Typo", body="Minor typo.", recommendation="Fix it."
            ),
        ]
    )
    result, json_path = run_validator(payload, tmp_path)

    assert result.returncode == 0, f"stderr: {result.stderr}"

    out = json.loads(json_path.read_text())
    ids = [f["id"] for f in out["findings"]]
    # IDs should be assigned (not missing)
    assert all(id_ for id_ in ids)
    # Critical comes before Low in severity order → C1 before L1
    assert ids == ["C1", "L1"]

    md_path = tmp_path / "round-1-findings.md"
    assert md_path.exists()
    md = md_path.read_text()
    assert "[C1]" in md
    assert "[L1]" in md


def test_verdict_aliases_are_normalized(tmp_path):
    payload = minimal_payload(verdict="NEEDS_REVISION")
    result, json_path = run_validator(payload, tmp_path)

    assert result.returncode == 0, f"stderr: {result.stderr}"

    out = json.loads(json_path.read_text())
    assert out["verdict"] == "needs-attention"


def test_a_numeric_confidence_string_becomes_a_number(tmp_path):
    payload = minimal_payload(
        findings=[
            minimal_finding(confidence="0.85"),
        ]
    )
    result, json_path = run_validator(payload, tmp_path)

    assert result.returncode == 0, f"stderr: {result.stderr}"

    out = json.loads(json_path.read_text())
    finding = out["findings"][0]
    # String "0.85" coerced to float
    assert finding["confidence"] == 0.85
    assert isinstance(finding["confidence"], float)


def test_a_confidence_label_is_dropped(tmp_path):
    payload = minimal_payload(
        findings=[
            minimal_finding(confidence="high"),
        ]
    )
    result, json_path = run_validator(payload, tmp_path)

    assert result.returncode == 0, f"stderr: {result.stderr}"

    out = json.loads(json_path.read_text())
    # Label confidence strings are deleted from output
    assert "confidence" not in out["findings"][0]


def test_a_missing_body_is_built_from_claim_evidence_and_impact(tmp_path):
    finding = {
        "severity": "high",
        "title": "Missing rate limiting",
        "recommendation": "Add rate limiting middleware.",
        "claim": "The API has no rate limiting.",
        "evidence": "No throttle decorator found.",
        "impact": "DoS vulnerability.",
    }
    # No "body" field supplied
    payload = minimal_payload(findings=[finding])
    result, json_path = run_validator(payload, tmp_path)

    assert result.returncode == 0, f"stderr: {result.stderr}"

    out = json.loads(json_path.read_text())
    body = out["findings"][0]["body"]
    # Body is synthesized from claim + evidence + impact
    assert "The API has no rate limiting." in body
    assert "No throttle decorator found." in body
    assert "DoS vulnerability." in body


def test_ids_the_reviewer_assigned_are_replaced(tmp_path):
    # Pre-assign an ID that should be stripped and re-assigned
    finding = minimal_finding(severity="medium")
    finding["id"] = "C1"  # Wrong prefix for severity; validator re-assigns
    payload = minimal_payload(findings=[finding])
    result, json_path = run_validator(payload, tmp_path)

    assert result.returncode == 0, f"stderr: {result.stderr}"

    out = json.loads(json_path.read_text())
    assigned_id = out["findings"][0]["id"]
    # Must be re-assigned deterministically based on severity (medium → M1)
    assert assigned_id == "M1"
    # The pre-supplied "C1" must not survive
    assert assigned_id != "C1"


def test_unknown_keys_are_set_aside_and_not_written(tmp_path):
    payload = minimal_payload()
    payload["metadata"] = {"generated_by": "codex", "round": 1}
    result, json_path = run_validator(payload, tmp_path)

    assert result.returncode == 0, f"stderr: {result.stderr}"

    out = json.loads(json_path.read_text())
    # Unknown key must not appear at top level in the final written JSON
    # (validator strips _extra before writing)
    assert "metadata" not in out
    assert "_extra" not in out


def test_ids_follow_severity_order(tmp_path):
    payload = minimal_payload(
        findings=[
            minimal_finding(severity="low", title="Low finding"),
            minimal_finding(severity="critical", title="Critical finding"),
            minimal_finding(severity="high", title="High finding"),
        ]
    )
    result, json_path = run_validator(payload, tmp_path)

    assert result.returncode == 0, f"stderr: {result.stderr}"

    out = json.loads(json_path.read_text())
    ids = [f["id"] for f in out["findings"]]
    # Severity order: critical → high → low; IDs assigned in that order
    assert ids == ["C1", "H1", "L1"]


def test_invalid_json_fails_with_a_message(tmp_path):
    json_path = tmp_path / "round-1-findings.json"
    json_path.write_text("{not valid json at all")
    result = subprocess.run([PYTHON, str(SCRIPT), str(json_path)], capture_output=True, text=True)

    assert result.returncode == 1
    assert "Invalid JSON" in result.stderr


def test_a_missing_required_field_fails_validation(tmp_path):
    # "verdict" is a required top-level field
    payload = {
        "summary": "Missing verdict field.",
        "findings": [minimal_finding()],
    }
    result, json_path = run_validator(payload, tmp_path)

    assert result.returncode == 1
    assert "Validation error" in result.stderr


def test_an_unknown_severity_fails_validation(tmp_path):
    payload = minimal_payload(
        findings=[
            minimal_finding(severity="blocker"),
        ]
    )
    result, json_path = run_validator(payload, tmp_path)

    assert result.returncode == 1
    assert "Validation error" in result.stderr


def test_approve_with_findings_warns_but_passes(tmp_path):
    payload = minimal_payload(
        verdict="approve",
        findings=[minimal_finding()],
    )
    result, json_path = run_validator(payload, tmp_path)

    # Should still succeed (non-fatal)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    # Warning must appear on stderr
    assert "Warning" in result.stderr
    assert "approve" in result.stderr


def test_null_line_fields_do_not_crash(tmp_path):
    """A finding with line_start/line_end both null must not crash the validator
    (regression: `None < None` TypeError) and is accepted as valid."""
    payload = minimal_payload(
        findings=[minimal_finding(line_start=None, line_end=None)],
    )
    result, _ = run_validator(payload, tmp_path)

    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "Traceback" not in result.stderr


def test_null_optional_fields_are_treated_as_absent(tmp_path):
    """Optional fields present with a null value (file/confidence/domain/lens/
    dimension_impact) are equivalent to absent — a cross-cutting finding must
    validate, not be rejected as a type error."""
    payload = minimal_payload(
        findings=[
            minimal_finding(
                file=None,
                line_start=None,
                line_end=None,
                confidence=None,
                domain=None,
                lens=None,
                dimension_impact=None,
            )
        ],
    )
    result, _ = run_validator(payload, tmp_path)

    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "must be a string" not in result.stderr


def test_a_reversed_line_range_still_fails_validation(tmp_path):
    """The null-guard must not disable the genuine line_end < line_start check."""
    payload = minimal_payload(
        findings=[minimal_finding(line_start=50, line_end=10)],
    )
    result, _ = run_validator(payload, tmp_path)

    assert result.returncode != 0
    assert "line_end" in result.stderr
