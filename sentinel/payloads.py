"""Handoff and rejection payload schemas (docs/00-overview-and-conventions.md).

Every agent transition must carry an `agent_handoff` YAML block in a comment; every
move to Rework must carry a `rework` block. These validators are the enforcement
point — an agent cannot transition through the tools without passing them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

VERDICTS = {"pass", "reject", "escalate"}
CHECK_RESULTS = {"pass", "fail", "n/a"}
SEVERITIES = {"blocker", "major", "minor"}
REJECTED_FROM = {"tech_review", "internal_review", "client_review"}

_FENCE = re.compile(r"(?:\{code[^}]*\}|```(?:yaml|yml)?)\s*\n(.*?)\n\s*(?:\{code\}|```)", re.DOTALL)


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    payload: dict | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and self.payload is not None


def extract_yaml_blocks(comment_body: str) -> list[dict]:
    """Pull every parseable fenced YAML block out of a Jira comment body.

    Accepts both markdown ``` fences and Jira wiki {code} macros, plus a bare
    document that starts with a known top-level key (agents sometimes post the
    YAML unfenced).
    """
    blocks: list[dict] = []
    for match in _FENCE.finditer(comment_body):
        try:
            data = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            continue
        if isinstance(data, dict):
            blocks.append(data)
    if not blocks and comment_body.lstrip().startswith(("agent_handoff:", "rework:")):
        try:
            data = yaml.safe_load(comment_body)
            if isinstance(data, dict):
                blocks.append(data)
        except yaml.YAMLError:
            pass
    return blocks


def find_payload(comment_body: str, top_key: str) -> dict | None:
    for block in extract_yaml_blocks(comment_body):
        if top_key in block and isinstance(block[top_key], dict):
            return block[top_key]
    return None


def validate_handoff(payload: dict | None) -> ValidationResult:
    """Validate an `agent_handoff` payload against the 00-overview schema."""
    result = ValidationResult()
    if not isinstance(payload, dict):
        result.errors.append("agent_handoff payload missing or not a mapping")
        return result

    for key in ("role", "ticket", "timestamp", "verdict", "from_status", "to_status"):
        if not str(payload.get(key) or "").strip():
            result.errors.append(f"agent_handoff.{key} is required")

    verdict = str(payload.get("verdict", "")).strip().lower()
    if verdict and verdict not in VERDICTS:
        result.errors.append(f"agent_handoff.verdict must be one of {sorted(VERDICTS)}, got '{verdict}'")

    checklist = payload.get("checklist")
    if not isinstance(checklist, list) or not checklist:
        result.errors.append("agent_handoff.checklist must be a non-empty list")
    else:
        for i, item in enumerate(checklist):
            if not isinstance(item, dict):
                result.errors.append(f"checklist[{i}] must be a mapping")
                continue
            if not str(item.get("id") or "").strip():
                result.errors.append(f"checklist[{i}].id is required")
            res = str(item.get("result", "")).strip().lower()
            if res not in CHECK_RESULTS:
                result.errors.append(f"checklist[{i}].result must be one of {sorted(CHECK_RESULTS)}")
            # Universal rule 5: every pass carries evidence
            if res == "pass" and not str(item.get("evidence") or "").strip():
                result.errors.append(f"checklist[{i}] ({item.get('id')}): result 'pass' requires evidence")

    # assumptions must be present as a list — empty means "none", absent means "didn't track"
    assumptions = payload.get("assumptions")
    if assumptions is None or not isinstance(assumptions, list):
        result.errors.append("agent_handoff.assumptions must be a list (empty list means 'none')")
    else:
        for i, a in enumerate(assumptions):
            if not isinstance(a, dict) or not str(a.get("claim") or "").strip():
                result.errors.append(f"assumptions[{i}] must be a mapping with a 'claim'")
            elif not str(a.get("verify_by") or "").strip():
                result.errors.append(f"assumptions[{i}] ('{a.get('claim')}') needs a verify_by path")

    if "outputs" in payload and not isinstance(payload.get("outputs"), dict):
        result.errors.append("agent_handoff.outputs must be a mapping")

    if not result.errors:
        result.payload = payload
    return result


def validate_rejection(payload: dict | None) -> ValidationResult:
    """Validate a `rework` payload against the 00-overview schema."""
    result = ValidationResult()
    if not isinstance(payload, dict):
        result.errors.append("rework payload missing or not a mapping")
        return result

    rejected_from = str(payload.get("rejected_from", "")).strip().lower()
    if rejected_from not in REJECTED_FROM:
        result.errors.append(f"rework.rejected_from must be one of {sorted(REJECTED_FROM)}")

    findings = payload.get("findings")
    if not isinstance(findings, list) or not findings:
        result.errors.append("rework.findings must be a non-empty list")
    else:
        for i, f in enumerate(findings):
            if not isinstance(f, dict):
                result.errors.append(f"findings[{i}] must be a mapping")
                continue
            severity = str(f.get("severity", "")).strip().lower()
            if severity not in SEVERITIES:
                result.errors.append(f"findings[{i}].severity must be one of {sorted(SEVERITIES)}")
            # "'I don't like it' is not a finding" — criterion_ref is mandatory
            if not str(f.get("criterion_ref") or "").strip():
                result.errors.append(f"findings[{i}] is missing criterion_ref")
            for key in ("id", "location", "description", "required_action"):
                if not str(f.get(key) or "").strip():
                    result.errors.append(f"findings[{i}].{key} is required")

    if not result.errors:
        result.payload = payload
    return result
