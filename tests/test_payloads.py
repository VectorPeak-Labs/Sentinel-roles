import yaml

from sentinel.payloads import (extract_yaml_blocks, find_payload, validate_handoff,
                               validate_rejection)

VALID_HANDOFF = {
    "role": "08-code-reviewer",
    "ticket": "SENT-42",
    "timestamp": "2026-07-12T10:00:00+00:00",
    "verdict": "pass",
    "from_status": "Tech Review",
    "to_status": "Tech Review Accepted",
    "checklist": [
        {"id": "REV-1", "result": "pass", "evidence": "https://jira/browse/SENT-42?focusedCommentId=1"},
        {"id": "REV-2", "result": "n/a"},
    ],
    "outputs": {"mr_url": "https://git/mr/7", "sec_gate": "green"},
    "assumptions": [
        {"claim": "CI cache was warm", "verify_by": "rerun clean @ 09-deployment"},
    ],
    "notes": "",
}

VALID_REJECTION = {
    "rejected_from": "tech_review",
    "rework_count": 1,
    "findings": [{
        "id": "F-1",
        "severity": "blocker",
        "criterion_ref": "SEC-2",
        "location": "api/auth.py:88",
        "description": "authz check missing on the new endpoint",
        "required_action": "enforce role check before handler body",
        "evidence": "https://jira/comment/9",
    }],
}


def test_valid_handoff_passes():
    assert validate_handoff(VALID_HANDOFF).ok


def test_handoff_pass_without_evidence_rejected():
    payload = yaml.safe_load(yaml.safe_dump(VALID_HANDOFF))
    payload["checklist"][0].pop("evidence")
    result = validate_handoff(payload)
    assert not result.ok
    assert any("evidence" in e for e in result.errors)


def test_handoff_missing_assumptions_rejected():
    payload = yaml.safe_load(yaml.safe_dump(VALID_HANDOFF))
    payload.pop("assumptions")
    result = validate_handoff(payload)
    assert not result.ok
    assert any("assumptions" in e for e in result.errors)


def test_handoff_empty_assumptions_allowed():
    payload = yaml.safe_load(yaml.safe_dump(VALID_HANDOFF))
    payload["assumptions"] = []
    assert validate_handoff(payload).ok


def test_handoff_assumption_without_verify_by_rejected():
    payload = yaml.safe_load(yaml.safe_dump(VALID_HANDOFF))
    payload["assumptions"] = [{"claim": "index exists on users.email"}]
    result = validate_handoff(payload)
    assert not result.ok
    assert any("verify_by" in e for e in result.errors)


def test_handoff_bad_verdict_rejected():
    payload = yaml.safe_load(yaml.safe_dump(VALID_HANDOFF))
    payload["verdict"] = "approved"
    assert not validate_handoff(payload).ok


def test_valid_rejection_passes():
    assert validate_rejection(VALID_REJECTION).ok


def test_rejection_without_criterion_ref_rejected():
    payload = yaml.safe_load(yaml.safe_dump(VALID_REJECTION))
    payload["findings"][0].pop("criterion_ref")
    result = validate_rejection(payload)
    assert not result.ok
    assert any("criterion_ref" in e for e in result.errors)


def test_rejection_bad_rejected_from():
    payload = yaml.safe_load(yaml.safe_dump(VALID_REJECTION))
    payload["rejected_from"] = "somewhere"
    assert not validate_rejection(payload).ok


def test_extract_from_markdown_fence():
    body = "Handoff below.\n\n```yaml\n" + yaml.safe_dump({"agent_handoff": VALID_HANDOFF}) + "```\n"
    assert find_payload(body, "agent_handoff")["ticket"] == "SENT-42"


def test_extract_from_jira_code_macro():
    body = "Handoff below.\n\n{code:yaml}\n" + yaml.safe_dump({"agent_handoff": VALID_HANDOFF}) + "{code}"
    assert find_payload(body, "agent_handoff")["ticket"] == "SENT-42"


def test_extract_ignores_broken_yaml():
    body = "```yaml\n: : not yaml : :\n```"
    assert extract_yaml_blocks(body) == []
    assert find_payload(body, "agent_handoff") is None
