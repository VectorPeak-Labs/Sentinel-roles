# Sentinel

An agent platform that drives a Jira-based development workflow end to end. It implements
the pipeline defined in [`docs/`](docs/README.md): thirteen roles (Intake → Business Analyst
→ Tech Lead → Refinement → Sprint Planner → Implementer → Code Review → Deploy → QA →
Client Review → Release, plus a Rework Router), run by an Orchestrator that watches the
board and dispatches LLM role agents.

- **Jira (self-hosted Server/Data Center)** is the single source of truth — no database.
  Leases, rework counters, waiting markers and deployed builds live in Jira issue
  *properties*; human-visible flags are labels; handoff/rejection payloads are YAML blocks
  in comments, exactly per `docs/00-overview-and-conventions.md`.
- **All AI calls** route through your **LiteLLM** deployment (OpenAI-compatible API).
- Everything runs as one container via **docker compose**.

## Quick start

```bash
cp .env.example .env        # fill in Jira PAT/domain + LiteLLM domain/key
docker compose run --rm doctor   # pre-flight: Jira, project statuses, LiteLLM, role docs
docker compose up -d --build
```

`doctor` is a **readiness gate**, not just a connectivity check. It classifies findings and
prints `READY: yes|no`, exiting non-zero until the deployment is safe and useful to run:

- **BLOCKERS** — will not run productively as configured: a missing role document, a blank
  shell-role command whose role would escalate on first use (e.g. `commands.deploy_production`
  empty → Release role 12 escalates on every release), an unreachable Jira/LiteLLM, or a
  workflow status that does not exist in your Jira.
- **WARNINGS** — runs, with a known risk: `WEBHOOK_SECRET` empty (mutating endpoints
  unauthenticated), `/health` exposure, the Code Reviewer sharing the default model.
- **INFO** — confirmations (roles loaded, statuses found, default model) and deferred checks.

```bash
python -m sentinel.doctor                 # human-readable readiness report
python -m sentinel.doctor --format json   # machine-readable {ready, blockers, warnings, info}
python -m sentinel.doctor --no-network    # config/command/security readiness only (offline)
```

Fix every **BLOCKER** before `docker compose up`; review **WARNINGS** for your deployment.

### Jira prerequisites

1. **PAT**: create a Personal Access Token for a dedicated Jira service account (Jira
   8.14+: profile → Personal Access Tokens). The account needs browse/edit/transition/
   comment/assign rights on the project. Its username becomes the agent identity used for
   leases.
2. **Workflow statuses** (rename in `config/pipeline.yml` if yours differ):
   `New`, `On Hold`, `Business Requirements`, `Technical Requirements`,
   `Technical Refinement`, `To Do`, `In Progress`, `Tech Review`, `Tech Review Accepted`,
   `Internal Review`, `Internal Review Accepted`, `Client Review`,
   `Client Review Accepted`, `Rework`, `Done`.
3. **Webhook** (optional but recommended — the 15-minute sweep works without it):
   Jira admin → System → WebHooks → URL
   `http://<sentinel-host>:8080/webhook/jira?token=<WEBHOOK_SECRET>`, events: issue
   created/updated + comment created, JQL filter `project = <YOUR_KEY>`.

### Day-to-day operation (the human levers)

Humans steer the pipeline entirely through Jira labels:

| Label | Meaning |
|---|---|
| `activate` | pull a ticket out of the icebox — triggers Intake (role 02) |
| `needs-human` | ticket frozen for a human decision; **remove it to resume** |
| `handoff-invalid` | orchestrator found an agent transition without a valid payload |
| `deploy-now` | force an immediate deploy batch (role 09) |
| `release-now` | open a production release window (role 12) — releases never fire on their own |

Agents ask questions and deliver packets as ticket comments; reply in comments and the
ticket wakes the responsible agent (via webhook, or on the next sweep). File evidence
(screenshots, scan reports, evidence bundles) is exchanged as ticket attachments, which
agents read and upload themselves.

Those labels steer individual tickets. To freeze the **whole** pipeline at once — an
incident, a bad model rollout, a maintenance window — use the pause control instead of
labelling every ticket or killing the container:

```bash
curl -X POST "http://<sentinel-host>:8080/pause?token=<WEBHOOK_SECRET>&reason=incident-1234"
curl -X POST "http://<sentinel-host>:8080/resume?token=<WEBHOOK_SECRET>"
```

While paused the Orchestrator dispatches no new agents (ticket or queue); agents already
running **drain to completion** rather than being killed mid-transition. The pause is
persisted to `DATA_DIR/pause.json`, so a container restart during an incident stays frozen
until you explicitly `/resume`. `GET /health` reports `"status": "paused"` with the reason.

The pause also backs a **daily LLM token budget** (circuit breaker for cost): set
`SENTINEL_LLM_DAILY_TOKEN_BUDGET` and the Orchestrator pauses the pipeline the moment a UTC
day's total token consumption crosses it (checked every sweep **and** on the webhook
fast-path), with a `pipeline_paused` alert naming the spend. Resume is deliberately manual —
a blown budget means something ran away (a rework loop, a chatty prompt), not that midnight
fixes it. `0` (default) disables. Watch it via `sentinel_llm_tokens_today` /
`sentinel_llm_daily_token_budget` on `/metrics` or the `llm` block of `/health`.

A third, fully **automatic** breaker covers LLM outages: after 3 consecutive failed LLM
calls the Orchestrator stops dispatching (each dispatch would just crash on its first chat
call, burn the ticket's retry budget and flood the board with spurious `needs-human`),
fires an `llm_outage` alert, and **probes the backend once per sweep** — the moment a probe
succeeds the gate lifts itself and an `llm_recovered` alert fires. No operator action
needed for transient outages; `/health` shows `llm.gated` and `/metrics` exposes
`sentinel_llm_gated` + `sentinel_llm_gate_engagements_total`.

## How it works

```
Jira webhooks ─┐                       ┌─> role agent (LLM tool loop over Jira)
               ├─> Orchestrator ───────┤     system prompt = docs/00 + docs/00a + role doc
15-min sweep ──┘    (traffic control)  └─> tools enforce the contracts
```

- **Orchestrator** (`sentinel/orchestrator.py`, role 01): dispatches the matching role per
  status, enforces WIP limits, reclaims dead leases (retry once → escalate), blocks any
  ticket with `rework_count > 2`, and validates that every agent transition carries a
  schema-valid `agent_handoff` payload (ORC-1…6). On shutdown (SIGTERM/redeploy) it cancels
  in-flight agents and gives them a grace window (`SENTINEL_SHUTDOWN_GRACE`, default 10 s) to
  release their leases — including tickets a queue role self-claimed — so a redeploy doesn't
  strand tickets `agent-leased` until the stale-lease timeout.
- **Resilient Jira access** (`sentinel/jira.py`): every Jira call retries transient
  failures (429/502/503/504 and network blips) with capped exponential backoff + jitter,
  honoring `Retry-After` — so a rate-limit or a brief Jira restart doesn't fail an agent
  action or flip `/health` to `degraded`. Mutating POSTs are never blindly retried on an
  ambiguous network error (no duplicate comments/transitions). Tune with
  `SENTINEL_JIRA_MAX_RETRIES` (default 3).
- **Role agents** (`sentinel/agent.py`): one instance per ticket (or per queue for
  Planner/Deploy/Release), loaded per the docs' loading contract, talking to Jira through
  a fixed tool set.
- **Contract enforcement** (`sentinel/tools.py`, `sentinel/payloads.py`): the *only* way an
  agent can transition a ticket is `transition_with_handoff`, which rejects payloads
  missing checklist evidence, assumptions, or verdicts; rejections additionally require a
  valid `rework` findings payload (severity + `criterion_ref` + required action on every
  finding). The Code Reviewer can run on a different model via `SENTINEL_REVIEWER_MODEL`.
- **Estimation poker** (role 05): the `run_estimators` tool spawns N blind, independent
  LLM estimator contexts; the convergence rule is applied by the refinement agent and
  ratification stays human.

## Configuration

- `.env` — secrets and endpoints (see `.env.example`).
- `config/pipeline.yml` — status→role dispatch table, WIP limits, labels, per-role model
  overrides, and **project commands** (clone/test/deploy/smoke/rollback). Both `docs/` and
  `config/` are volume-mounted read-only; edit and `docker compose restart sentinel`.

### What you must fill in per project

The shell-enabled roles (Implementer 07, Reviewer 08, Deploy 09, QA 10, Release 12) run
real commands in a persistent workspace. These workspaces live on the fixed `/data` volume
(next to the audit log and pause state), so set `SENTINEL_WORKSPACE_MAX_BYTES` to keep them
from ever filling it: each sweep, any **idle** role workspace over the cap is wiped (roles
with a running agent are never touched, and project commands must tolerate a fresh
workspace — clone-if-missing — exactly as they must on a new container). `0` (default)
disables; wipes are audited (`workspace_wiped`) and counted (`sentinel_workspace_wipes_total`).
Until you fill in the `commands:` section of
`config/pipeline.yml` (repo clone, test suite, deploy scripts), those agents will escalate
with `needs-human` when they need them — by design, they never guess at deploy commands.

### Alerting (getting pinged when the pipeline needs you)

Every escalation writes a Jira comment and the `needs-human` label. To be actively
notified instead of watching the board, set `SENTINEL_ALERT_WEBHOOK_URL` to a chat webhook
(a Slack incoming-webhook URL works as-is). Sentinel then POSTs a JSON message on every
event that needs a human — agent and orchestrator escalations, plus pipeline pause/resume:

```json
{"text": "🚨 SENT SENT-42 escalated by 09-deployment — needs a human. deploy_production command not configured → fill in config/pipeline.yml",
 "event": "agent_escalation", "ticket": "SENT-42",
 "url": "https://jira.example.com/browse/SENT-42", "source": "09-deployment"}
```

Slack renders `text`; generic consumers get the structured `event`/`ticket`/`url` fields.
Alerting is **disabled by default** and **best-effort** — a slow or failing endpoint is
logged and never blocks or crashes the pipeline. Leave the URL unset to keep Jira comments
as the only channel (or wire your own automation on the `needs-human` label instead).

For Prometheus-side alerting — recommended alert rules for every `sentinel_*` signal
(LLM outage, sweep failures, needs-human backlog, token burn, invalid handoffs), the
circuit-breaker overview, and per-alert runbook notes — see **[OPERATIONS.md](OPERATIONS.md)**.

Escalations also **re-alert if a human forgets them**: each sweep re-surfaces any ticket
left frozen (`needs-human`/`handoff-invalid`) and untouched for longer than
`SENTINEL_STALE_ESCALATION_HOURS` (default 24 h), at most once per window per ticket. This
upholds ORC-1 ("nothing silently stuck") even when the first alert goes unanswered. Set the
value to `0` to disable the reminders.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness + pause state + LiteLLM health + currently running agents (no auth) |
| `GET /metrics` | Prometheus metrics: dispatch/escalation/reclaim/sweep-failure counters + live gauges (no auth) |
| `GET /audit?ticket=…&event=…&limit=…` | query the audit trail (newest matching records, across rotated generations; auth required) |
| `POST /webhook/jira` | Jira webhook receiver |
| `POST /sweep` | force an immediate board sweep |
| `POST /pause?reason=…` | freeze all dispatch (in-flight runs drain); survives restart |
| `POST /resume` | lift the pause and resume dispatching |

`GET /metrics` exposes the Prometheus text format for scraping — monotonic counters
(`sentinel_dispatches_total`, `sentinel_escalations_total`, `sentinel_lease_reclaims_total`,
`sentinel_sweep_failures_total`, `sentinel_transitions_validated_total`,
`sentinel_handoff_invalid_total`), process gauges (`sentinel_paused`,
`sentinel_running_agents`, `sentinel_consecutive_sweep_failures`, `sentinel_sweeps_total`, …),
and **board-backlog gauges** refreshed each sweep: `sentinel_tickets_in_status{status="…"}`
(queue depth per stage), `sentinel_needs_human_tickets`, `sentinel_handoff_invalid_tickets`,
`sentinel_agent_tickets_total`, plus **LLM token-usage counters** labeled by pipeline role
and model: `sentinel_llm_calls_total{role,model}`, `sentinel_llm_prompt_tokens_total{role,model}`,
`sentinel_llm_completion_tokens_total{role,model}` (every agent action is a billed LLM call —
these make per-role consumption and runaway loops visible; totals reset on restart, so use
`rate()`/`increase()`). Point a Prometheus scraper at it to alert on escalation
spikes, sustained sweep failures, a growing Rework/To-Do backlog, escalations left
unresolved (`needs_human_tickets > 0` for too long), or an abnormal token-burn rate.

The four `POST` endpoints can freeze or nudge the whole pipeline, so they require the
`WEBHOOK_SECRET`. Present it as an `X-Sentinel-Token: <secret>` header, an
`Authorization: Bearer <secret>` header (both keep it out of URLs and access logs), or a
`?token=<secret>` query param (Jira webhooks can only put it in the URL). The check is
constant-time. If `WEBHOOK_SECRET` is unset the endpoints are **open** and Sentinel logs a
startup warning — set it in production. `GET /health` is always unauthenticated (liveness).

Audit trail: `docker compose exec sentinel tail -f /data/audit.jsonl` — every dispatch,
transition, reclaim and escalation (mirrored to Jira comments where the docs require it).
The file is size-rotated (`audit.jsonl.1 … .N`, default 50 MB × 5 generations) so it can't
fill the `/data` volume; tune with `SENTINEL_AUDIT_MAX_BYTES` / `SENTINEL_AUDIT_BACKUP_COUNT`
(set max bytes to `0` for a single unbounded file).

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest tests -q         # payload-contract + config/dispatch-table tests
python -m sentinel.doctor          # readiness gate against real Jira/LiteLLM (needs .env vars)
```
