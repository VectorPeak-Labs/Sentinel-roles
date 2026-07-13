"""Attachment tools: the evidence channel (upload, download, workspace confinement)."""

import asyncio
import json

from sentinel.audit import AuditLog
from sentinel.config import load_settings
from sentinel.lease import LeaseManager
from sentinel import tools as toolsmod
from sentinel.tools import ToolContext, dispatch, tools_for_role

from fakes import FakeJira, FakeLLM

REQUIRED_ENV = {
    "JIRA_BASE_URL": "https://jira.example.com",
    "JIRA_PAT": "pat",
    "JIRA_PROJECT_KEY": "SENT",
    "LITELLM_BASE_URL": "https://llm.example.com",
    "LITELLM_API_KEY": "sk",
}


def make_ctx(monkeypatch, tmp_path, role_id="10-qa", workspace=None):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    settings = load_settings("config/pipeline.yml")
    jira = FakeJira()
    return ToolContext(
        jira=jira, llm=FakeLLM(),
        leases=LeaseManager(jira, "sentinel-bot", "agent-leased", 1800),
        settings=settings, audit=AuditLog(tmp_path / "audit.jsonl"),
        role=settings.roles[role_id], ticket="SENT-1",
        workspace=workspace or tmp_path / "ws")


def call(ctx, name, **args):
    return asyncio.run(dispatch(ctx, name, args))


def test_attachment_tools_available_to_every_role(monkeypatch, tmp_path):
    ctx = make_ctx(monkeypatch, tmp_path, role_id="02-intake-triage")
    names = {t["function"]["name"] for t in tools_for_role(ctx.role)}
    assert {"get_attachment", "attach_file"} <= names


def test_get_ticket_lists_attachment_metadata(monkeypatch, tmp_path):
    ctx = make_ctx(monkeypatch, tmp_path)
    asyncio.run(ctx.jira.upload_attachment("SENT-1", "spec.pdf", b"%PDF", "application/pdf"))
    out = json.loads(call(ctx, "get_ticket", key="SENT-1").content)
    assert out["attachments"] == [{
        "id": "1", "filename": "spec.pdf", "size": 4,
        "mime_type": "application/pdf", "author": None, "created": None}]


def test_attach_inline_content(monkeypatch, tmp_path):
    ctx = make_ctx(monkeypatch, tmp_path)
    result = call(ctx, "attach_file", key="SENT-1", content="AC-1: pass",
                  filename="evidence.txt")
    assert "attached 'evidence.txt'" in result.content
    att = ctx.jira.attachments["SENT-1"][0]
    assert att["data"] == b"AC-1: pass"
    assert att["mimeType"] == "text/plain"


def test_attach_workspace_file_defaults_filename(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    (ws / "shots").mkdir(parents=True)
    (ws / "shots" / "before.png").write_bytes(b"\x89PNG")
    ctx = make_ctx(monkeypatch, tmp_path, workspace=ws)
    result = call(ctx, "attach_file", key="SENT-1", path="shots/before.png")
    assert "attached 'before.png'" in result.content
    assert ctx.jira.attachments["SENT-1"][0]["mimeType"] == "image/png"


def test_attach_path_escape_blocked(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    (tmp_path / "ws-evil").mkdir()
    (tmp_path / "ws-evil" / "x.txt").write_text("evil")
    ctx = make_ctx(monkeypatch, tmp_path, workspace=ws)
    for path in ("../secret.txt", "../ws-evil/x.txt"):
        result = call(ctx, "attach_file", key="SENT-1", path=path)
        assert result.content.startswith("ERROR")
        assert "workspace" in result.content
    assert "SENT-1" not in ctx.jira.attachments


def test_attach_requires_exactly_one_source(monkeypatch, tmp_path):
    ctx = make_ctx(monkeypatch, tmp_path)
    assert call(ctx, "attach_file", key="SENT-1").content.startswith("ERROR")
    (ctx.workspace).mkdir(parents=True)
    (ctx.workspace / "a.txt").write_text("a")
    both = call(ctx, "attach_file", key="SENT-1", path="a.txt", content="b",
                filename="a.txt")
    assert both.content.startswith("ERROR")
    no_name = call(ctx, "attach_file", key="SENT-1", content="text only")
    assert no_name.content.startswith("ERROR")
    assert "filename" in no_name.content


def test_attach_size_limit(monkeypatch, tmp_path):
    ctx = make_ctx(monkeypatch, tmp_path)
    monkeypatch.setattr(toolsmod, "MAX_ATTACHMENT_BYTES", 8)
    result = call(ctx, "attach_file", key="SENT-1", content="way too large",
                  filename="big.txt")
    assert result.content.startswith("ERROR")
    assert "limit" in result.content


def test_get_attachment_returns_text_inline_and_saves(monkeypatch, tmp_path):
    ctx = make_ctx(monkeypatch, tmp_path)
    asyncio.run(ctx.jira.upload_attachment("SENT-1", "notes.txt",
                                           b"login form spec", "text/plain"))
    result = call(ctx, "get_attachment", key="SENT-1", attachment_id="1")
    assert "login form spec" in result.content
    assert (ctx.workspace / "attachments" / "notes.txt").read_bytes() == b"login form spec"


def test_get_attachment_binary_saved_with_sanitized_name(monkeypatch, tmp_path):
    ctx = make_ctx(monkeypatch, tmp_path)
    # A hostile filename must not escape workspace/attachments
    asyncio.run(ctx.jira.upload_attachment("SENT-1", "../../evil.png",
                                           b"\x89PNG", "image/png"))
    result = call(ctx, "get_attachment", key="SENT-1", attachment_id="1")
    assert "--- content ---" not in result.content
    assert (ctx.workspace / "attachments" / "evil.png").exists()
    assert not (tmp_path / "evil.png").exists()


def test_get_attachment_unknown_id(monkeypatch, tmp_path):
    ctx = make_ctx(monkeypatch, tmp_path)
    asyncio.run(ctx.jira.upload_attachment("SENT-1", "a.txt", b"a", "text/plain"))
    result = call(ctx, "get_attachment", key="SENT-1", attachment_id="99")
    assert result.content.startswith("ERROR")
    assert "1:a.txt" in result.content
