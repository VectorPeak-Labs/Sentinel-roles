"""HTTP entrypoint: Jira webhook receiver + health/status endpoints.

The orchestrator loop runs as a background task in the same process; the
15-minute board sweep makes webhooks optional (but they make the pipeline
react in seconds instead of minutes).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from . import __version__
from .audit import AuditLog
from .config import load_settings
from .jira import JiraClient
from .llm import LLM
from .metrics import Metrics, render as render_metrics
from .notify import Notifier
from .orchestrator import Orchestrator

log = logging.getLogger("sentinel")

settings = load_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

jira = JiraClient(settings.jira_base_url, settings.jira_pat,
                  max_retries=settings.jira_max_retries)
llm = LLM(settings.litellm_base_url, settings.litellm_api_key, settings.default_model)
audit = AuditLog(settings.data_dir / "audit.jsonl",
                 max_bytes=settings.audit_max_bytes,
                 backup_count=settings.audit_backup_count)
notifier = Notifier(settings.alert_webhook_url, settings.jira_base_url)
metrics = Metrics()
orchestrator = Orchestrator(settings, jira, llm, audit, notifier, metrics)


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop_task = asyncio.create_task(orchestrator.run_forever(), name="orchestrator-loop")
    try:
        yield
    finally:
        await orchestrator.stop()
        loop_task.cancel()
        await jira.close()
        await llm.close()
        await notifier.close()


# After this many consecutive failed LLM calls, the LiteLLM backend is treated as
# down and /health reports 'degraded' (each failure already survived the client's
# internal retries, so a run of them is a real outage, not a transient blip).
LLM_DEGRADED_AFTER = 3

app = FastAPI(title="Sentinel", version=__version__, lifespan=lifespan)

if not settings.webhook_secret:
    log.warning("WEBHOOK_SECRET is not set — /webhook/jira, /sweep, /pause and /resume "
                "are UNAUTHENTICATED. Anyone who can reach this port can freeze the "
                "pipeline. Set WEBHOOK_SECRET in production.")


def _authorized(secret: str, token: str, x_sentinel_token: str | None,
                authorization: str | None) -> bool:
    """Constant-time check of a presented secret against the configured one.

    The secret may arrive as the `X-Sentinel-Token` header, an
    `Authorization: Bearer <token>` header (both keep it out of URLs and access
    logs), or the `token` query param (Jira webhooks can only put it in the URL).
    An empty configured secret means the endpoints are open (documented mode).
    """
    if not secret:
        return True
    presented = ""
    if x_sentinel_token:
        presented = x_sentinel_token
    elif authorization and authorization[:7].lower() == "bearer ":
        presented = authorization[7:].strip()
    elif token:
        presented = token
    return bool(presented) and hmac.compare_digest(presented, secret)


async def require_auth(
    token: str = "",
    x_sentinel_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency guarding the mutating endpoints (constant-time)."""
    if not _authorized(settings.webhook_secret, token, x_sentinel_token, authorization):
        raise HTTPException(status_code=403, detail="bad token")


# Strong references to fire-and-forget tasks: asyncio only keeps weak refs to
# running tasks, so an unreferenced webhook handler could be GC'd mid-flight.
_background: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


@app.get("/health")
async def health() -> dict:
    llm_ok = llm.consecutive_failures < LLM_DEGRADED_AFTER
    if not orchestrator.agent_user:
        status = "starting"
    elif orchestrator.paused:
        status = "paused"     # operator freeze — no new dispatch until /resume
    elif orchestrator.consecutive_sweep_failures >= 2 or not llm_ok:
        # Jira unreachable / PAT expired, or the LiteLLM backend is failing — either
        # way agents can't make progress and every dispatch just escalates.
        status = "degraded"
    else:
        status = "ok"
    return {
        "status": status,
        "version": __version__,
        "agent_user": orchestrator.agent_user,
        "paused": orchestrator.paused,
        "paused_at": orchestrator.paused_at,
        "pause_reason": orchestrator.pause_reason,
        "last_sweep_at": orchestrator.last_sweep_at,
        "last_sweep_error": orchestrator.last_sweep_error,
        "consecutive_sweep_failures": orchestrator.consecutive_sweep_failures,
        "sweep_count": orchestrator.sweep_count,
        "pending_webhook_evaluations": len(orchestrator._pending_keys),
        "llm": {
            "ok": llm_ok,
            "consecutive_failures": llm.consecutive_failures,
            "last_error": llm.last_error,
            "last_ok_at": llm.last_ok_at,
        },
        "running_agents": [
            {"role": role_id, "ticket": ticket}
            for (role_id, ticket) in orchestrator.running
        ],
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics() -> str:
    """Prometheus exposition: monotonic counters plus live gauges sampled from
    orchestrator state. Unauthenticated, like /health — scrape it on a trusted
    network (or terminate/authenticate at the reverse proxy)."""
    board = orchestrator.board_state
    gauges = {
        "up": ("1 when the process is serving.", 1),
        "paused": ("1 when the pipeline is paused (no dispatch).", int(orchestrator.paused)),
        "running_agents": ("Role agents currently running.", len(orchestrator.running)),
        "consecutive_sweep_failures":
            ("Consecutive failed sweeps (>=2 => degraded).", orchestrator.consecutive_sweep_failures),
        "sweeps_total": ("Board sweeps completed since start.", orchestrator.sweep_count),
        "pending_webhook_evaluations":
            ("Tickets queued for a debounced webhook evaluation.", len(orchestrator._pending_keys)),
        # Board backlog, refreshed each sweep.
        "agent_tickets_total":
            ("Tickets in agent-owned statuses at the last sweep.", board["total"]),
        "needs_human_tickets":
            ("Tickets frozen awaiting a human (needs-human).", board["needs_human"]),
        "handoff_invalid_tickets":
            ("Tickets flagged handoff-invalid.", board["handoff_invalid"]),
        # LiteLLM backend health (passive: updated by real agent/doctor calls).
        "llm_up": ("1 when recent LLM calls are succeeding.",
                   int(llm.consecutive_failures < LLM_DEGRADED_AFTER)),
        "llm_consecutive_failures":
            ("Consecutive failed LLM calls since the last success.", llm.consecutive_failures),
    }
    labeled = {
        "tickets_in_status": (
            "Tickets in each agent-owned status at the last sweep.",
            [({"status": s}, n) for s, n in sorted(board["by_status"].items())]),
    }
    return render_metrics(metrics.snapshot(), gauges, labeled)


@app.post("/webhook/jira")
async def jira_webhook(request: Request, _: None = Depends(require_auth)) -> dict:
    try:
        event = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    # Handle asynchronously — Jira expects a fast 200.
    _spawn_background(orchestrator.handle_webhook(event))
    return {"accepted": True}


@app.post("/sweep")
async def trigger_sweep(_: None = Depends(require_auth)) -> dict:
    """Manually trigger a board sweep (same auth as the webhook)."""
    _spawn_background(orchestrator.sweep())
    return {"sweeping": True}


@app.post("/pause")
async def pause(reason: str = "", _: None = Depends(require_auth)) -> dict:
    """Freeze the pipeline: stop dispatching new agents (in-flight runs drain).

    The freeze is persisted, so a container restart stays paused until /resume.
    Same auth as the webhook.
    """
    await orchestrator.pause(reason=reason, by="api")
    return {"paused": True, "reason": orchestrator.pause_reason,
            "paused_at": orchestrator.paused_at}


@app.post("/resume")
async def resume(_: None = Depends(require_auth)) -> dict:
    """Lift a pause and resume dispatching on the next sweep/webhook."""
    await orchestrator.resume(by="api")
    return {"paused": False}
