# _INDEX.md — Project Analysis Index

> Purpose of this file: bring a fresh AI agent (or human) to the same understanding of this
> repository as a full "analyse the project" pass, without re-reading every file. It covers
> what the system is, how it is architected, what every file does, the core protocols and
> invariants, and how to run/test/extend it. Written against commit `5fb3306` (July 2026).

---

## 1. What this project is

**Sentinel** is a self-hosted **multi-agent platform that drives a Jira-based software
development workflow end to end**. Thirteen pipeline roles (Intake → Business Analyst →
Tech Lead → Refinement → Sprint Planner → Implementer → Code Reviewer → Deployment → QA →
Client Review Facilitator → Release, plus a Rework Router) are each run as an **LLM agent
in a tool loop over Jira**, dispatched and policed by an **Orchestrator** process.

Three foundational design decisions shape everything:

1. **Jira is the single source of truth — there is no database.**
   - Machine state (leases, rework counters, retry counters, waiting markers, deployed
     builds) lives in **Jira issue properties** (`sentinel.*` keys — no custom-field admin needed).
   - Human-visible flags are **labels** (`agent-leased`, `needs-human`, `activate`, …).
   - Inter-role contracts (handoff/rejection payloads) are **YAML blocks inside Jira comments**.
   - Targets **self-hosted Jira Server / Data Center** (REST API v2, PAT bearer auth,
     `assignee` by username — *not* Jira Cloud).
2. **All LLM calls go through a LiteLLM deployment** (OpenAI-compatible chat-completions
   API) — the platform is model-agnostic; per-role model overrides are config.
3. **Contracts are enforced in code, not just described in prompts.** An agent *cannot*
   transition a ticket without a schema-valid handoff payload; a rejection *cannot* reach
   Rework without valid findings. The tool layer refuses and returns the exact validation
   errors.

The runtime ships as **one Docker container** (FastAPI + background orchestrator loop) via
`docker compose`.

## 2. Repository layout

```
├── README.md                  # top-level: quick start, Jira prerequisites, human levers, endpoints
├── _INDEX.md                  # this file
├── sentinel/                  # the Python package (~1.9k lines, Python 3.12, asyncio)
│   ├── __init__.py            # docstring + __version__ ("0.1.0")
│   ├── server.py              # FastAPI app: /health, /webhook/jira, /sweep; starts the orchestrator loop
│   ├── orchestrator.py        # role 01: sweep + webhook dispatch, leases, WIP, loop-breaker, handoff audit
│   ├── agent.py               # AgentRunner: builds system prompts, runs the LLM tool loop, heartbeats
│   ├── tools.py               # all 17 agent tools + their enforcement logic (the contract layer)
│   ├── payloads.py            # agent_handoff / rework YAML schema validators + comment extraction
│   ├── jira.py                # async Jira Server/DC client (httpx); issue-property state keys
│   ├── lease.py               # LeaseManager: claim / heartbeat / release / reclaim protocol
│   ├── llm.py                 # thin AsyncOpenAI wrapper pointed at LiteLLM
│   ├── config.py              # env settings + config/pipeline.yml loader (RoleConfig, Settings)
│   ├── audit.py               # append-only JSONL audit log (thread-locked)
│   └── doctor.py              # pre-flight CLI: Jira/project/statuses/LiteLLM/role-doc checks
├── config/pipeline.yml        # THE dispatch table: role triggers, WIP limits, labels, models, project commands
├── docs/                      # role goal documents — these ARE the agents' system prompts
│   ├── README.md              # loading contract: agent = 00 + 00a + own role doc, in that order
│   ├── 00-overview-and-conventions.md   # pipeline table, universal rules, payload schemas, lease/escalation protocols
│   ├── 00a-operating-manual.md          # reasoning craft: 8 disciplines + 5-question self-test before every handoff
│   ├── 01-orchestrator.md … 13-rework-router.md   # one doc per role: mission, trigger, procedure, checklist ids, end states
├── tests/                     # pytest suite (in-memory fakes, no network)
│   ├── fakes.py               # FakeJira + FakeLLM (scripted tool-call responses)
│   ├── test_config.py         # dispatch table ↔ docs pipeline parity, env expansion, required env vars
│   ├── test_payloads.py       # handoff/rejection schema rules, fence extraction ({code} + ```)
│   ├── test_lease.py          # claim/heartbeat/release/reclaim + staleness boundaries
│   ├── test_orchestrator.py   # dispatch gating (labels/lease/WIP/retries/waiting), webhook debounce, ORC-5 validation
│   ├── test_agent_loop.py     # full tool-loop runs: happy path, invalid payload retry, turn cap, crash, queue roles
│   ├── test_rework_router.py  # increment_rework idempotency + loop-breaker signalling
│   └── test_tools_reject.py   # reject_to_rework pre-flight ordering (no orphaned payloads)
│   └── test_run_command.py    # workspace containment (path-aware, not string-prefix)
├── conftest.py                # inserts repo root into sys.path (bare `pytest` support)
├── Dockerfile                 # python:3.12-slim + git/curl (for shell roles); entrypoint serve|doctor
├── docker-compose.yml         # sentinel service (port 8080, docs+config mounted ro, /data volume) + doctor profile
├── entrypoint.sh              # serve → uvicorn sentinel.server:app :8080 ; doctor → python -m sentinel.doctor
├── .env.example               # all env vars, documented (see §6)
├── requirements.txt           # fastapi, uvicorn, httpx, pyyaml, openai — pinned to majors
└── .github/workflows/ci.yml   # pip install + `pytest tests -q` on push/PR (Python 3.12)
```

## 3. The pipeline (docs/00 table)

| # | Role | Consumes status | Produces (success) | Trigger type | Shell? |
|---|------|-----------------|--------------------|--------------|--------|
| 01 | Orchestrator | all (loop) | — | continuous | — |
| 02 | Intake & Triage | New / On Hold **+ `activate` label** | Business Requirements | ticket | no |
| 03 | Business Analyst | Business Requirements | Technical Requirements | ticket | no |
| 04 | Tech Lead Debrief | Technical Requirements | Technical Refinement | ticket | no |
| 05 | Refinement & Estimation | Technical Refinement | To Do | ticket (`run_estimators` tool) | no |
| 06 | Sprint Planner | To Do | In Progress | **queue**, gated on In-Progress WIP capacity | no |
| 07 | Implementer | In Progress | Tech Review | ticket | **yes** |
| 08 | Code Reviewer (Security Gate 1) | Tech Review | Tech Review Accepted | ticket, optional separate model (`SENTINEL_REVIEWER_MODEL`) | **yes** |
| 09 | Deployment | Tech Review Accepted / Internal Review Accepted | Internal Review / Client Review | **queue**, 1800 s batch cadence (`deploy-now` bypasses) | **yes** |
| 10 | QA (Security Gate 2) | Internal Review | Internal Review Accepted | ticket | **yes** |
| 11 | Client Review Facilitator | Client Review | Client Review Accepted | ticket | no |
| 12 | Release | Client Review Accepted | Done | **queue**, gated on `release-now` label (never fires alone) | **yes** |
| 13 | Rework Router | Rework | In Progress (fix-brief) | ticket (`increment_rework` tool) | no |

Review roles (08, 10, 11) may also produce **Rework**. Statuses are configurable names in
`config/pipeline.yml` (case-insensitive matching); the 15 expected statuses are listed in
README.md §Jira prerequisites.

## 4. Runtime architecture & control flow

```
Jira webhooks ─┐                          ┌─> AgentRunner._loop (LLM tool loop, ≤80 turns)
               ├─> Orchestrator ──dispatch┤     system prompt = docs/00 + docs/00a + role doc + runtime preamble
15-min sweep ──┘   (traffic control only) └─> tools.py enforces payload contracts on every transition
```

1. **`server.py`** loads settings at import, builds `JiraClient`, `LLM`, `AuditLog`,
   `Orchestrator`; the FastAPI lifespan starts `orchestrator.run_forever()` as a background
   task. Endpoints: `GET /health` (status: starting/ok/degraded — degraded after ≥2
   consecutive sweep failures), `POST /webhook/jira?token=…`, `POST /sweep?token=…`.
   Webhook handling is fire-and-forget with strong task references (asyncio GC pitfall).
2. **`orchestrator.py`** — startup retries with backoff (Jira may boot alongside). Every
   `sweep_interval` (900 s) it JQL-searches all agent-owned statuses (ORDER BY updated ASC,
   ≤500) and evaluates each ticket + the queue roles. Webhook events are **debounced 2 s**
   into one evaluation pass per burst. Per-ticket dispatch gate order (`_evaluate_ticket`):
   `needs-human`/`handoff-invalid` label → role match (+ `require_label`) → already running →
   **active lease skip / stale lease reclaim + retry bump** → **retry limit** (count > 1 ⇒
   escalate & reset counter so removing `needs-human` grants a fresh budget) → **rework
   loop-breaker** (role 13, count > `rework_limit`=2 ⇒ escalate) → **waiting marker** (parked
   on a human; wakes on newer `updated` or `wake_at`) → **WIP limit** per status. Queue
   roles get one singleton instance with a ticket listing; conditions:
   `capacity_in_progress`, `release_window`, `min_interval_seconds` (+ force label).
   `_on_status_change` (ORC-5): an **agent** transition without a matching valid
   `agent_handoff` in the last 10 comments ⇒ label `handoff-invalid` + escalate (never
   reverted); a **human** transition is logged and honored (universal rule 6); a clean
   handoff resets the retry counter.
3. **`agent.py`** — `build_system_prompt` concatenates `docs/00` + `docs/00a` + role doc +
   a runtime preamble (identity, labels, hard rules, queue rules, shell commands from
   `pipeline.yml`). The loop: LLM call → serialize only canonical tool-call shape (LiteLLM
   replay compatibility) → dispatch tools → terminal tool ends the run. Prose without tool
   calls gets a reminder message. Turn cap (80) or crash ⇒ release leases, bump
   `sentinel.retries`, comment; the orchestrator then retries once, then escalates.
   A heartbeat task refreshes every held lease (own ticket + queue-claimed) every 600 s;
   a lost lease means a human/orchestrator intervened — stop touching that ticket.
4. **`tools.py`** — the enforcement layer (see §5). Terminal tools:
   `transition_with_handoff`, `escalate`, `finish_run` (terminal only for the role's own
   ticket in ticket-scoped roles). Tool errors return `ERROR: …` strings to the model,
   never crash the run.

## 5. Agent tools (tools.py)

Base tools (all roles): `get_ticket` (fields + sentinel state + last 30 comments),
`search_tickets` (JQL, auto-scoped to project), `add_comment`, `set_labels`
(cannot touch the `agent-leased` label), `create_ticket`, `link_tickets`, `assign_ticket`,
`set_deployed_build`, `transition_with_handoff`, `reject_to_rework`, `escalate`, `finish_run`.

Conditional: `claim_ticket`/`release_ticket` (queue roles), `increment_rework` (role 13),
`run_command` (shell roles), `run_estimators` (role 05).

Key enforcement details:

- **`transition_with_handoff`** is the ONLY status-change path. `_check_transition`
  pre-flights everything **before posting anything**: queue-role ownership, YAML parse,
  `validate_handoff` schema, ticket/key match, `to_status` match, `from_status` matches
  the *live* status ("someone moved it — re-read"), and **the Jira workflow actually has an
  edge to the target** (otherwise an orphaned payload comment would be posted per retry).
  On success: post summary + `{code:yaml}` payload comment → transition → delete waiting +
  retries properties → release lease.
- **`reject_to_rework`** validates the `rework` payload AND pre-flights the handoff before
  posting either; posts the rejection payload first (the Router's input), then delegates to
  `transition_with_handoff`.
- **`increment_rework`** (role 13) reads the newest `rework` payload from comments and is
  **idempotent across router retries**: it stores `last_counted_comment` in the
  `sentinel.rework` property so a crashed/retried run never double-counts a bounce.
  Returns `limit_exceeded` so the agent knows to escalate instead of dispatching.
- **`run_command`** runs in a persistent per-role workspace (`DATA_DIR/workspace/<role>`),
  with a **path-aware containment check** (`Path.is_relative_to`, not string prefix —
  "07" vs "07-evil"), timeout capped at 1800 s, output truncated at 30 000 chars.
- **`run_estimators`** spawns ≤5 blind, independent LLM contexts (temperature 1.0) for
  planning poker; convergence is applied by the refinement agent, ratification by a human.

## 6. State model & payload schemas

**Jira issue properties** (keys in `jira.py`):

| Property | Content |
|---|---|
| `sentinel.lease` | `{agent, role, started, heartbeat}` — active lease |
| `sentinel.rework` | `{count, rejected_from, last_counted_comment, history[]}` |
| `sentinel.waiting` | `{since, reason, role, wake_at}` — parked on a human, wake on activity or timeout (default 24 h) |
| `sentinel.deployed` | `{<env>: {build, at, by}}` per test/staging/production |
| `sentinel.retries` | `{count}` — crash/reclaim/turn-cap retries per stage |

**`agent_handoff` payload** (validated by `payloads.validate_handoff`): required `role`,
`ticket`, `timestamp`, `verdict` (pass|reject|escalate), `from_status`, `to_status`;
`checklist` non-empty, each item with an `id`, result in pass|fail|n/a, and **`pass`
requires `evidence`** (universal rule 5); `assumptions` must be a **list, present even if
empty** ("empty means none, absent means didn't track"), each with `claim` + `verify_by`;
optional `outputs` mapping.

**`rework` payload** (`validate_rejection`): `rejected_from` ∈ {tech_review,
internal_review, client_review}; `findings` non-empty, each with `id`, severity ∈
{blocker, major, minor}, **mandatory `criterion_ref`** ("'I don't like it' is not a
finding"), `location`, `description`, `required_action`.

Payload extraction (`extract_yaml_blocks`) accepts markdown ``` fences, Jira `{code}`
macros, and bare documents starting with a known top-level key.

**Labels (the human levers)** — `activate` (pull from icebox → triggers Intake),
`needs-human` (frozen; **remove to resume**), `handoff-invalid` (invalid agent transition),
`agent-leased` (managed by LeaseManager only), `deploy-now` (bypass deploy batch cadence),
`release-now` (open a production release window). All renameable in `pipeline.yml`.

**Lease protocol** (`lease.py`): claim = property + label + assignee + comment; fails if an
unexpired lease exists (staleness = no heartbeat within `SENTINEL_LEASE_TIMEOUT`, 1800 s);
heartbeat every 600 s; release deletes property + label (idempotent); orchestrator reclaim
posts an explanatory comment.

## 7. Configuration

**Environment** (`.env`, see `.env.example`): required `JIRA_BASE_URL`, `JIRA_PAT`,
`JIRA_PROJECT_KEY`, `LITELLM_BASE_URL` (`/v1` auto-appended), `LITELLM_API_KEY`.
Optional: `SENTINEL_DEFAULT_MODEL` (default `gpt-4o`), `SENTINEL_REVIEWER_MODEL`,
`WEBHOOK_SECRET`, `DATA_DIR` (/data), `DOCS_DIR` (docs), `SENTINEL_CONFIG`
(config/pipeline.yml), `SENTINEL_SWEEP_INTERVAL` (900), `SENTINEL_LEASE_TIMEOUT` (1800),
`SENTINEL_HEARTBEAT_INTERVAL` (600), `SENTINEL_MAX_AGENT_TURNS` (80), `SENTINEL_LOG_LEVEL`.

**`config/pipeline.yml`** supports `${VAR}` / `${VAR:default}` expansion (recursive, done
in `config._expand_env`). Defines: `rework_limit` (2), `split_threshold_points` (8),
`labels`, `wip_limits` per status, the `roles:` dispatch table (trigger type/statuses/
require_label/condition, `shell`, `model`, `estimators`, `min_interval_seconds`), and
**`commands:`** — project-specific clone/test/deploy_test/deploy_staging/deploy_production/
smoke_test/rollback strings injected into shell-role prompts. **All command strings are
empty by default; shell roles escalate with `needs-human` rather than guess** — filling
these in is the per-project onboarding step.

`docs/` and `config/` are volume-mounted read-only in compose; edit +
`docker compose restart sentinel` to apply.

## 8. How to run / test / verify

```bash
# Development
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest tests -q            # 60+ tests, no network, in-memory fakes

# Pre-flight against real infra (needs .env values exported)
python -m sentinel.doctor            # checks config, role docs, Jira, statuses, LiteLLM

# Production
cp .env.example .env                 # fill in Jira PAT/domain + LiteLLM domain/key
docker compose run --rm doctor
docker compose up -d --build
docker compose exec sentinel tail -f /data/audit.jsonl   # audit trail
```

CI (`.github/workflows/ci.yml`) runs the pytest suite on Python 3.12 for every push/PR.
There is no linter/formatter config in the repo. Tests use `FakeJira`/`FakeLLM`
(`tests/fakes.py`) — `FakeLLM` replays scripted tool-call sequences, so agent-loop behavior
is tested end to end without a model.

## 9. Invariants & design decisions worth knowing before changing code

- **ORC-1…6 invariants** (docs/01): every agent-status ticket has exactly one active lease
  or `needs-human`; no lease outlives its heartbeat timeout; WIP limits hold; no
  `rework_count > 2` ticket is worked; every agent transition has a valid payload.
- **Humans always win**: human transitions are never validated, reverted, or
  counter-transitioned; a lost lease means back off.
- **Never post before pre-flight**: both transition tools validate everything (including
  workflow-edge existence) before writing any comment, to avoid orphaned/duplicate payloads
  that the Rework Router would later parse.
- **Idempotency under retries**: rework counting keys off the rejection comment id;
  a clean stage exit (validated handoff or successful transition) resets `sentinel.retries`.
- **Fail-safe loop ends**: every agent run ends via exactly one terminal tool, the turn
  cap, or the crash handler — all three release leases and leave a retry breadcrumb.
- **Escalation is the designed fallback everywhere** (missing commands, missing workflow
  edges, ambiguity, rework loops, repeated crashes): label + comment + freeze, human
  removes the label to resume.
- **The docs are runtime artifacts, not documentation**: editing `docs/*.md` changes agent
  behavior directly (they are the system prompts). `docs/00a-operating-manual.md` is the
  reasoning-quality layer (8 disciplines + a 5-question self-test) loaded into every agent.
- Jira **Server/DC v2 API only** (PAT bearer, username-based assignee, `{code}` comment
  macros); search uses POST /search with 50-per-page pagination.

## 10. Current state & known gaps

- Version 0.1.0; single project key per deployment; one container, no horizontal scaling
  (concurrency is per-status WIP limits inside one asyncio process).
- `commands:` in `pipeline.yml` are intentionally blank — the platform is generic until a
  project fills them in.
- Notifications = Jira comments + `needs-human` label only; external alerting (chat pings)
  is expected to be wired via Jira automation on that label.
- No auth on `/health`; webhook/sweep share one token; webhook is plain HTTP in examples.
- Git history: initial docs (`docs/` first), then the platform build, then a hardening
  series (pagination/GC fixes, agent-loop tests, lease heartbeats for queue claims,
  pre-flight rejection ordering, idempotent rework counting, degraded-health surfacing,
  webhook debounce, workspace containment fix) merged via PR #1.
