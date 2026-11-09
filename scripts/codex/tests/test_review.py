"""Review deliverables: the output schema, the three files a review leaves, and their rejections."""

import json
from typing import Any

import pytest
from codex_jobs import findings, review, runner

REVIEW_MD = "## Summary\nOne real problem.\n\n## Review Findings\n- the loader skips validation\n"
FINDINGS_VERDICT_MD = "## Findings\nNone.\n\n## Verdict\nReady\n"


def finding(**overrides: Any) -> dict[str, Any]:
    base = {
        "severity": "high",
        "title": "Loader skips tenant check",
        "body": "The loader reads rows without the tenant filter.",
        "recommendation": "Filter by tenant before reading.",
        "file": "app/loader.py",
        "line_start": 12,
        "line_end": 14,
        "confidence": 0.8,
        "category": "security",
    }
    return {**base, **overrides}


def answer(**overrides: Any) -> dict[str, Any]:
    base = {
        "status": "completed",
        "review": REVIEW_MD,
        "verdict": "needs-attention",
        "summary": "One high finding.",
        "findings": [
            finding(),
            finding(severity="critical", title="Open endpoint", file="", line_start=0, line_end=0),
        ],
    }
    return {**base, **overrides}


def test_review_schema_is_strict_at_every_level():
    def check(schema: dict[str, Any]) -> None:
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
            for sub in schema["properties"].values():
                check(sub)
        if schema.get("type") == "array":
            check(schema["items"])

    check(review.load_schema())


def test_answer_matches_the_schema_and_a_blocker_does_not():
    schema = review.load_schema()
    assert runner.validate_instance(answer(), schema) == []
    problems = runner.validate_instance(answer(findings=[finding(severity="Blocker")]), schema)
    assert problems == [
        "$.findings[0].severity must be one of ['critical', 'high', 'medium', 'low'], got 'Blocker'"
    ]


def test_blocker_that_reaches_the_validator_is_rejected_not_rendered(tmp_path):
    outcome = review.write_deliverables(tmp_path, answer(findings=[finding(severity="Blocker")]))
    assert not outcome.task_succeeded
    assert any("invalid severity 'blocker'" in problem for problem in outcome.problems)
    assert not (tmp_path / "findings.md").exists()
    assert not (tmp_path / "findings.json").exists()
    assert (tmp_path / "review.md").exists()


def test_findings_rendered_from_json(tmp_path):
    outcome = review.write_deliverables(tmp_path, answer())
    assert outcome.task_succeeded
    assert outcome.files == review.DELIVERABLES
    data = json.loads((tmp_path / "findings.json").read_text())
    assert [(f["id"], f["severity"]) for f in data["findings"]] == [
        ("C1", "critical"),
        ("H1", "high"),
    ]
    assert "file" not in data["findings"][0] and "line_start" not in data["findings"][0]
    assert (tmp_path / "findings.md").read_text() == findings.render_markdown(data)


def test_review_md_is_the_markdown_as_written(tmp_path):
    review.write_deliverables(tmp_path, answer(review=REVIEW_MD + "\n\n"))
    assert (tmp_path / "review.md").read_text() == REVIEW_MD


@pytest.mark.parametrize("status", ["blocked", "failed"])
def test_incomplete_review_keeps_its_files_and_does_not_succeed(tmp_path, status):
    outcome = review.write_deliverables(
        tmp_path,
        answer(
            status=status, review="## Summary\nBlocked.\n", findings=[], verdict="needs-attention"
        ),
    )
    assert not outcome.task_succeeded
    assert outcome.review_approved is None
    assert outcome.problems == (f"review {status}",)
    assert all((tmp_path / name).exists() for name in review.DELIVERABLES)


@pytest.mark.parametrize(
    "markdown, accepted",
    [
        (REVIEW_MD, True),
        (FINDINGS_VERDICT_MD, True),
        ("## Summary\nok\n", False),
        ("No headings at all.\n", False),
        ("  \n", False),
    ],
)
def test_completed_review_needs_the_review_headings(tmp_path, markdown, accepted):
    outcome = review.write_deliverables(
        tmp_path, answer(review=markdown, findings=[], verdict="approve")
    )
    assert outcome.task_succeeded is accepted
    assert (tmp_path / "findings.md").exists()


@pytest.mark.parametrize("verdict, approved", [("approve", True), ("needs-attention", False)])
def test_review_approved_follows_the_verdict_of_a_completed_review(tmp_path, verdict, approved):
    outcome = review.write_deliverables(tmp_path, answer(verdict=verdict, findings=[]))
    assert outcome.review_approved is approved


def test_findings_the_validator_rejects_leave_no_findings_files(tmp_path):
    outcome = review.write_deliverables(
        tmp_path, answer(findings=[finding(line_start=20, line_end=10)])
    )
    assert not outcome.task_succeeded
    assert any("line_end (10) < line_start (20)" in problem for problem in outcome.problems)
    assert outcome.files == ("review.md",)
