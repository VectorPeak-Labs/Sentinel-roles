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

`doctor` verifies connectivity and that every pipeline status exists in your Jira workflow
before anything runs.

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

## How it works

```
Jira webhooks ─┐                       ┌─> role agent (LLM tool loop over Jira)
               ├─> Orchestrator ───────┤     system prompt = docs/00 + docs/00a + role doc
15-min sweep ──┘    (traffic control)  └─> tools enforce the contracts
```

- **Orchestrator** (`sentinel/orchestrator.py`, role 01): dispatches the matching role per
  status, enforces WIP limits, reclaims dead leases (retry once → escalate), blocks any
  ticket with `rework_count > 2`, and validates that every agent transition carries a
  schema-valid `agent_handoff` payload (ORC-1…6).
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
real commands in a persistent workspace. Until you fill in the `commands:` section of
`config/pipeline.yml` (repo clone, test suite, deploy scripts), those agents will escalate
with `needs-human` when they need them — by design, they never guess at deploy commands.
Escalation notifications are Jira comments + the `needs-human` label; wire your own chat
alert on that label (e.g. Jira automation) if you want pings.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness + pause state + currently running agents |
| `POST /webhook/jira?token=…` | Jira webhook receiver |
| `POST /sweep?token=…` | force an immediate board sweep |
| `POST /pause?token=…&reason=…` | freeze all dispatch (in-flight runs drain); survives restart |
| `POST /resume?token=…` | lift the pause and resume dispatching |

Audit trail: `docker compose exec sentinel tail -f /data/audit.jsonl` — every dispatch,
transition, reclaim and escalation (mirrored to Jira comments where the docs require it).

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest tests -q         # payload-contract + config/dispatch-table tests
python -m sentinel.doctor          # pre-flight against real Jira/LiteLLM (needs .env vars)
```
