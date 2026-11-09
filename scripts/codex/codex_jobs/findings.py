#!/usr/bin/env python3
"""Validate a review's findings JSON, number the findings and render them as Markdown.

Usage:
    python3 findings.py path/to/findings.json

Input: a JSON object with `verdict`, `summary` and `findings` (each finding needs `severity`,
`title`, `body` and `recommendation`). `normalize` first repairs common drift: verdict aliases,
confidence labels, a missing body, unknown keys (kept aside under `_extra`).

On success (exit 0):
  - Assigns deterministic IDs (C1, H1, M1, L1, ...) sorted by severity
  - Writes validated JSON back (with IDs added)
  - Renders `<name>.md` beside the JSON

On failure (exit 1):
  - Prints each error to stderr
  - Writes no output file, so the caller can set the JSON aside and keep the review's prose

`review.py` imports the same functions for Codex review jobs.
"""

import json
import sys
from pathlib import Path

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
SEVERITY_PREFIX = {"critical": "C", "high": "H", "medium": "M", "low": "L"}
REQUIRED_TOP = {"verdict", "summary", "findings"}
REQUIRED_FINDING = {"severity", "title", "body", "recommendation"}
VALID_VERDICTS = {"approve", "needs-attention"}
VALID_CATEGORIES = {
    "correctness",
    "security",
    "performance",
    "architecture",
    "tests",
    "docs",
    "style",
    "ux",
    "tech-debt",
}
VALID_LENSES = {"correctness", "security", "operational"}
VALID_DIMENSIONS = {"completeness", "actionability", "consistency", "recoverability", "specificity"}
SUPPORTED_SCHEMA_VERSIONS = {1, 2}

VERDICT_ALIASES = {
    "NEEDS_REVISION": "needs-attention",
    "needs_revision": "needs-attention",
    "needs_attention": "needs-attention",
    "NEEDS_ATTENTION": "needs-attention",
    "needs-revision": "needs-attention",
    "APPROVE": "approve",
}


def normalize(data: dict) -> dict:
    """Best-effort normalization of common format drift before validation."""
    # Normalize verdict
    verdict = data.get("verdict", "")
    if verdict in VERDICT_ALIASES:
        data["verdict"] = VERDICT_ALIASES[verdict]

    # Stash unknown top-level keys. schema_version, next_steps, rubric_score
    # are first-class top-level fields and must survive normalization.
    known_top = REQUIRED_TOP | {"schema_version", "next_steps", "rubric_score", "_extra"}
    extra = {k: data.pop(k) for k in list(data.keys()) if k not in known_top}
    if extra:
        data["_extra"] = extra

    if "findings" not in data or not isinstance(data["findings"], list):
        return data

    for finding in data["findings"]:
        # Lowercase severity
        if "severity" in finding and isinstance(finding["severity"], str):
            finding["severity"] = finding["severity"].lower()

        # Coerce string confidence to float, or delete if label
        if "confidence" in finding and isinstance(finding["confidence"], str):
            conf_str = finding["confidence"].strip()
            # Label strings: delete entirely (synthesized values distort convergence)
            if conf_str.lower() in ("high", "medium", "low"):
                del finding["confidence"]
            else:
                try:
                    finding["confidence"] = float(conf_str)
                except ValueError:
                    del finding["confidence"]

        # Synthesize body from claim+evidence+impact if body is missing
        if "body" not in finding or not finding.get("body", "").strip():
            parts = []
            if finding.get("claim"):
                parts.append(finding["claim"])
            if finding.get("evidence"):
                ev = finding["evidence"]
                if isinstance(ev, list):
                    parts.append(" ".join(str(e) for e in ev))
                else:
                    parts.append(str(ev))
            if finding.get("impact"):
                parts.append(f"Impact: {finding['impact']}")
            if parts:
                finding["body"] = " ".join(parts)

        # Strip pre-assigned ID (validator assigns deterministically)
        finding.pop("id", None)

        # Stash unknown finding keys. Schema v2 adds domain/lens/dimension_impact
        # as first-class fields that must pass through normalization unchanged.
        known_finding = {
            "severity",
            "title",
            "body",
            "recommendation",
            "file",
            "line_start",
            "line_end",
            "confidence",
            "category",
            "domain",
            "lens",
            "dimension_impact",
        }
        finding_extra = {k: finding.pop(k) for k in list(finding.keys()) if k not in known_finding}
        if finding_extra:
            finding["_extra"] = finding_extra

    return data


def validate(data: dict) -> list[str]:
    """Return list of validation errors (empty = valid)."""
    errors = []

    # Schema version contract (Phase 2a.1): v2 is current, v1 accepted for
    # legacy findings files (treated as missing schema_version). Any other
    # value is a hard fail — silent "assume latest" couples parsers to drift.
    schema_version = data.get("schema_version")
    if schema_version is not None and schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(
            f"Unsupported schema_version: {schema_version!r} "
            f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )

    for field in REQUIRED_TOP:
        if field not in data:
            errors.append(f"Missing required top-level field: {field}")

    if "verdict" in data and data["verdict"] not in VALID_VERDICTS:
        errors.append(f"Invalid verdict: {data['verdict']}")

    # Warn on unknown top-level keys (non-fatal)
    known_top = REQUIRED_TOP | {"schema_version", "next_steps", "rubric_score", "_extra"}
    for key in data:
        if key not in known_top:
            print(f"Warning: unknown top-level key '{key}'", file=sys.stderr)

    if "findings" in data:
        if not isinstance(data["findings"], list):
            errors.append("'findings' must be an array")
        else:
            for i, finding in enumerate(data["findings"]):
                # Optional fields present with a null value are equivalent to
                # absent — reviewers routinely emit null for inapplicable fields
                # on cross-cutting findings (no single file/line). Drop them so
                # the per-field type checks below don't reject a legitimate null.
                # Required fields are left intact so a null there is still caught.
                if isinstance(finding, dict):
                    for opt in (
                        "file",
                        "line_start",
                        "line_end",
                        "confidence",
                        "category",
                        "domain",
                        "lens",
                        "dimension_impact",
                    ):
                        if opt in finding and finding[opt] is None:
                            del finding[opt]
                for field in REQUIRED_FINDING:
                    if field not in finding:
                        errors.append(f"Finding [{i}]: missing required field '{field}'")
                if "severity" in finding and finding["severity"] not in SEVERITY_ORDER:
                    errors.append(f"Finding [{i}]: invalid severity '{finding['severity']}'")
                # Category validation (optional field — missing is allowed)
                if "category" in finding and finding["category"] not in VALID_CATEGORIES:
                    errors.append(
                        f"Finding [{i}]: invalid category '{finding['category']}' "
                        f"(must be one of: {', '.join(sorted(VALID_CATEGORIES))})"
                    )
                # Semantic checks
                if "confidence" in finding:
                    c = finding["confidence"]
                    if not isinstance(c, (int, float)) or c < 0.0 or c > 1.0:
                        errors.append(f"Finding [{i}]: confidence must be 0.0-1.0, got {c}")
                # line_start/line_end are optional and may be null (cross-cutting
                # findings with no single line). Only range-check when both are
                # real integers — a bare None must not crash the comparison.
                ls = finding.get("line_start")
                le = finding.get("line_end")
                if isinstance(ls, int) and isinstance(le, int):
                    if le < ls:
                        errors.append(f"Finding [{i}]: line_end ({le}) < line_start ({ls})")
                for str_field in ("title", "body", "recommendation"):
                    if str_field in finding and (
                        not isinstance(finding[str_field], str) or not finding[str_field].strip()
                    ):
                        errors.append(f"Finding [{i}]: '{str_field}' must be a non-empty string")
                if "file" in finding and not isinstance(finding["file"], str):
                    errors.append(
                        f"Finding [{i}]: 'file' must be a string, got {type(finding['file']).__name__}"
                    )
                # Schema v2 fields (optional). domain is free-form; lens and
                # dimension_impact are enumerated.
                if "domain" in finding and not isinstance(finding["domain"], str):
                    errors.append(f"Finding [{i}]: 'domain' must be a string")
                if "lens" in finding and finding["lens"] not in VALID_LENSES:
                    errors.append(
                        f"Finding [{i}]: invalid lens '{finding['lens']}' "
                        f"(must be one of: {', '.join(sorted(VALID_LENSES))})"
                    )
                if "dimension_impact" in finding:
                    di = finding["dimension_impact"]
                    if not isinstance(di, list):
                        errors.append(f"Finding [{i}]: 'dimension_impact' must be an array")
                    else:
                        for d in di:
                            if d not in VALID_DIMENSIONS:
                                errors.append(
                                    f"Finding [{i}]: invalid dimension_impact entry '{d}' "
                                    f"(must be one of: {', '.join(sorted(VALID_DIMENSIONS))})"
                                )

    return errors


def assign_ids(findings: list[dict]) -> list[dict]:
    """Assign deterministic IDs sorted by severity then original order."""
    decorated = [
        (SEVERITY_ORDER.get(f.get("severity", "low"), 3), i, f) for i, f in enumerate(findings)
    ]
    decorated.sort(key=lambda x: (x[0], x[1]))

    counters: dict[str, int] = {}
    for _, _, finding in decorated:
        sev = finding.get("severity", "low")
        prefix = SEVERITY_PREFIX[sev]
        counters[prefix] = counters.get(prefix, 0) + 1
        finding["id"] = f"{prefix}{counters[prefix]}"

    return [f for _, _, f in decorated]


def render_markdown(data: dict) -> str:
    """Render findings JSON into the bracketed-ID markdown format."""
    lines = ["## Review Findings", ""]

    sections = [
        ("Critical (must fix before implementation)", "critical"),
        ("High (should fix, significant impact)", "high"),
        ("Medium (nice to fix, moderate impact)", "medium"),
        ("Low (minor, optional)", "low"),
    ]

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}

    for heading, severity in sections:
        lines.append(f"### {heading}")
        findings = [f for f in data["findings"] if f.get("severity") == severity]
        if not findings:
            lines.append("None")
        else:
            for f in findings:
                fid = f.get("id", "?")
                # A finding is one line: line breaks in the text would end it early
                title = " ".join(f["title"].split())
                body = " ".join(f["body"].split())
                rec = " ".join(f["recommendation"].split())
                loc = ""
                if f.get("file"):
                    loc = f" ({f['file']}"
                    if f.get("line_start"):
                        loc += f":{f['line_start']}"
                        if f.get("line_end"):
                            loc += f"-{f['line_end']}"
                    loc += ")"
                cat = f" ({f['category']})" if f.get("category") else ""
                lines.append(f"- [{fid}]{cat} **{title}**{loc}: {body} | Recommendation: {rec}")
                counts[severity] += 1
        lines.append("")

    overall = "pass"
    if counts["critical"] > 0:
        overall = "major-rework"
    elif counts["high"] > 0:
        overall = "needs-work"

    lines.append("## Summary")
    lines.append(
        f"- Critical: {counts['critical']}, High: {counts['high']}, "
        f"Medium: {counts['medium']}, Low: {counts['low']}"
    )
    lines.append(f"- Overall: {overall}")
    lines.append(f"- Verdict: {data['verdict']}")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: findings.py <path/to/findings.json>", file=sys.stderr)
        sys.exit(1)

    json_path = Path(sys.argv[1])
    if not json_path.exists():
        print(f"File not found: {json_path}", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(json_path.read_text())
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    data = normalize(data)
    errors = validate(data)
    if errors:
        for err in errors:
            print(f"Validation error: {err}", file=sys.stderr)
        sys.exit(1)

    # Warn on suspicious but non-fatal conditions
    if data.get("verdict") == "approve" and data.get("findings"):
        print(
            f"Warning: verdict is 'approve' but {len(data['findings'])} findings present",
            file=sys.stderr,
        )

    data["findings"] = assign_ids(data["findings"])

    # Strip internal processing keys before writing
    data.pop("_extra", None)
    for finding in data.get("findings", []):
        finding.pop("_extra", None)

    # Write validated JSON (with IDs)
    json_path.write_text(json.dumps(data, indent=2) + "\n")

    # Render markdown
    md_path = json_path.with_suffix(".md")
    md_path.write_text(render_markdown(data))

    total = len(data["findings"])
    print(f"Validated {total} findings, wrote {md_path.name}")


if __name__ == "__main__":
    main()
