"""Review jobs: the output contract Codex answers under and the files a completed review leaves.

A native review (`review/start`, Codex's built-in reviewer) answers in prose and leaves only
`review.md`; `parse_target` reads its `--target`.

A review turn answers with the JSON object in `schemas/review.schema.json`: the review as
Markdown (`review`) plus the same findings as structured data. `write_deliverables` turns that
answer into three files in a directory:

    review.md       the Markdown review, as Codex wrote it
    findings.json   the findings, normalized, validated and id-assigned by `findings.py`
    findings.md     `findings.json` rendered by `findings.py`

`findings.py` also runs as a script on any reviewer's findings JSON, so a Codex review's findings
are numbered and rendered the same way as everyone else's.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import findings as validator

PACKAGE_ROOT = Path(__file__).resolve().parents[1]  # scripts/codex
SCHEMA_PATH = PACKAGE_ROOT / "schemas" / "review.schema.json"

DELIVERABLES = ("review.md", "findings.json", "findings.md")

TARGET_FORMS = "uncommitted | base:<branch> | commit:<sha>"
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")

# `schema_version` of the findings contract the answer is written under (see `findings.py`).
FINDINGS_SCHEMA_VERSION = 2

# Heading sets a completed review must carry: Summary with Review Findings, or Findings with Verdict.
REQUIRED_HEADINGS = ({"Summary", "Review Findings"}, {"Findings", "Verdict"})

# The strict output schema makes every finding field required, so a finding with no single
# location carries these in place of the field.
NO_FILE = ""
NO_LINE = 0

REVIEW_CONTRACT = (
    "OUTPUT CONTRACT: Return the JSON object required by the output schema. Set status to completed "
    "only if you read the prompt and performed the requested review. Set status to blocked if tool "
    "access or missing inputs prevent completing the review, or failed for another execution failure; "
    "then set verdict to needs-attention, findings to an empty array and explain why in review. Status "
    "describes review completion, not whether the reviewed code is ready to merge. Put the complete "
    "Markdown review in review, following the prompt's response format or, if none, using '## Summary' "
    "and '## Review Findings'. Put the same findings in findings, one item each: severity is exactly "
    "critical, high, medium or low (a Blocker is critical); title, body and recommendation are "
    "non-empty; file is the repository path, or an empty string when the finding has no single file; "
    "line_start and line_end are 1-based, or 0 when the finding has no single location; confidence is "
    "a number from 0 to 1; category is the closest of the schema's values. Set verdict to approve only "
    "when findings is empty. This envelope takes precedence over instructions about final-message "
    "formatting in the prompt. Do not create, modify, or delete repository files or change git state."
)


@dataclass(frozen=True)
class ReviewOutcome:
    """What the review asks of the job's result.

    `task_succeeded` is False for a review that is blocked or failed, lacks the required headings,
    or carries findings the validator rejects; `problems` then says why. `review_approved` is
    None unless the review completed, and False for a completed review that needs attention.
    """

    task_succeeded: bool
    review_approved: bool | None
    problems: tuple[str, ...]
    files: tuple[str, ...]


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def parse_target(text: str) -> dict[str, str]:
    """The `review/start` target for a `--target` value (`TARGET_FORMS`); `ValueError` for anything else."""
    if text == "uncommitted":
        return {"type": "uncommittedChanges"}
    kind, _, value = text.partition(":")
    if kind == "base" and value.strip():
        return {"type": "baseBranch", "branch": value}
    if kind == "commit" and _SHA_RE.fullmatch(value):
        return {"type": "commit", "sha": value}
    raise ValueError(f"unrecognized review target {text!r}; use {TARGET_FORMS}")


def render_review_output(output: dict[str, Any]) -> str:
    """The prose of a native review from the structured output its sub-thread ended with (`overall_explanation`, `findings`).

    Reproduces the text the app-server delivers as the review's agent message: the explanation, then
    `Review comment:` (one finding) or `Full review comments:` and one `- title — path:start-end`
    item per finding with its body indented two spaces. KeyError when `output` lacks a field.
    """
    sections = []
    if explanation := output["overall_explanation"].strip():
        sections.append(explanation)
    findings = output["findings"]
    if findings:
        items = []
        for finding in findings:
            location = finding["code_location"]
            span = location["line_range"]
            head = f"- {finding['title']} \u2014 {location['absolute_file_path']}:{span['start']}-{span['end']}"
            items.append(
                head + "\n" + "\n".join(f"  {line}" for line in finding["body"].split("\n"))
            )
        header = "Review comment:" if len(findings) == 1 else "Full review comments:"
        sections.append(header + "\n\n" + "\n\n".join(items))
    return "\n\n".join(sections)


def lacks_required_headings(review: str) -> bool:
    """Whether `review` lacks both accepted heading sets."""
    headings = set(re.findall(r"^## ([^\n]+?)\s*$", review, re.MULTILINE))
    return not any(required <= headings for required in REQUIRED_HEADINGS)


def _findings_document(answer: dict[str, Any]) -> dict[str, Any]:
    """The answer's verdict, summary and findings as the validator's input, locations the schema forced dropped."""
    findings = []
    for finding in answer["findings"]:
        finding = dict(finding)
        if finding["file"] == NO_FILE:
            del finding["file"]
        for name in ("line_start", "line_end"):
            if finding[name] == NO_LINE:
                del finding[name]
        findings.append(finding)
    return {
        "schema_version": FINDINGS_SCHEMA_VERSION,
        "verdict": answer["verdict"],
        "summary": answer["summary"],
        "findings": findings,
    }


def write_deliverables(directory: Path, answer: dict[str, Any]) -> ReviewOutcome:
    """Write the review's files into `directory` from an `answer` that satisfies the review schema.

    `review.md` is always written. `findings.json` and `findings.md` are written whenever the
    validator accepts the findings, a blocked or failed review included, so its files stay for
    the caller to read; a rejected finding leaves them out and is reported in `problems`.
    """
    directory = Path(directory)
    problems: list[str] = []
    files = ["review.md"]

    review = answer["review"]
    (directory / "review.md").write_text(review.rstrip() + "\n")
    if answer["status"] != "completed":
        problems.append(f"review {answer['status']}")
    elif not review.strip():
        problems.append("review is empty")
    elif lacks_required_headings(review):
        problems.append("review.md missing required sections")

    document = validator.normalize(_findings_document(answer))
    errors = validator.validate(document)
    if errors:
        problems += [f"findings rejected: {error}" for error in errors]
    else:
        document["findings"] = validator.assign_ids(document["findings"])
        document.pop("_extra", None)
        for finding in document["findings"]:
            finding.pop("_extra", None)
        findings_json = json.dumps(document, indent=2) + "\n"
        (directory / "findings.json").write_text(findings_json)
        (directory / "findings.md").write_text(validator.render_markdown(json.loads(findings_json)))
        files += ["findings.json", "findings.md"]

    completed = answer["status"] == "completed"
    return ReviewOutcome(
        task_succeeded=not problems,
        review_approved=(answer["verdict"] == "approve") if completed else None,
        problems=tuple(problems),
        files=tuple(files),
    )


def write_native_deliverable(directory: Path, text: str) -> ReviewOutcome:
    """Write a native review's message as `review.md`. It has no findings, so no verdict (`review_approved` None)."""
    (Path(directory) / "review.md").write_text(text.rstrip() + "\n")
    return ReviewOutcome(
        task_succeeded=True, review_approved=None, problems=(), files=("review.md",)
    )
