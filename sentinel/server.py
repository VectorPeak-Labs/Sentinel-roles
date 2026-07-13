"""HTTP entrypoint: Jira webhook receiver + health/status endpoints.

The orchestrator loop runs as a background task in the same process; the
15-minute board sweep makes webhooks optional (but they make the pipeline
react in seconds instead of minutes).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from . import __version__
from .audit import AuditLog
from .config import load_settings
from .jira import JiraClient
from .llm import LLM
from .orchestrator import Orchestrator

log = logging.getLogger("sentinel")

settings = load_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

jira = JiraClient(settings.jira_base_url, settings.jira_pat)
llm = LLM(settings.litellm_base_url, settings.litellm_api_key, settings.default_model)
audit = AuditLog(settings.data_dir / "audit.jsonl")
orchestrator = Orchestrator(settings, jira, llm, audit)


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


app = FastAPI(title="Sentinel", version=__version__, lifespan=lifespan)

# Strong references to fire-and-forget tasks: asyncio only keeps weak refs to
# running tasks, so an unreferenced webhook handler could be GC'd mid-flight.
_background: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


@app.get("/health")
async def health() -> dict:
    if not orchestrator.agent_user:
        status = "starting"
    elif orchestrator.consecutive_sweep_failures >= 2:
        status = "degraded"   # Jira unreachable / PAT expired — dispatching is halted
    else:
        status = "ok"
    return {
        "status": status,
        "version": __version__,
        "agent_user": orchestrator.agent_user,
        "last_sweep_at": orchestrator.last_sweep_at,
        "last_sweep_error": orchestrator.last_sweep_error,
        "consecutive_sweep_failures": orchestrator.consecutive_sweep_failures,
        "sweep_count": orchestrator.sweep_count,
        "pending_webhook_evaluations": len(orchestrator._pending_keys),
        "running_agents": [
            {"role": role_id, "ticket": ticket}
            for (role_id, ticket) in orchestrator.running
        ],
    }


@app.post("/webhook/jira")
async def jira_webhook(request: Request, token: str = "") -> dict:
    if settings.webhook_secret and token != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="bad token")
    try:
        event = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    # Handle asynchronously — Jira expects a fast 200.
    _spawn_background(orchestrator.handle_webhook(event))
    return {"accepted": True}


@app.post("/sweep")
async def trigger_sweep(token: str = "") -> dict:
    """Manually trigger a board sweep (same auth as the webhook)."""
    if settings.webhook_secret and token != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="bad token")
    _spawn_background(orchestrator.sweep())
    return {"sweeping": True}
