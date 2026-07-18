# Sentinel Project Vision

## One-sentence vision

Sentinel turns Jira into a governed, self-hosted AI delivery line: every software ticket moves from intake to production through role-specialized agents, enforced handoff contracts, auditable evidence, and explicit human control points.

## Why this project exists

Software teams do not primarily fail because they lack another chat assistant. They fail because intent, requirements, implementation evidence, review findings, deployment state, and client approval are scattered across tools and people. AI can accelerate that work, but without process rails it also accelerates guessing, scope drift, rubber-stamp reviews, and silent production risk.

Sentinel exists to make AI useful inside a real delivery workflow by treating the workflow as the product:

- **Jira remains the system of record.** Humans keep working in the board they already trust; Sentinel stores leases, waits, rework counters, deployed builds, and handoff evidence in Jira issue properties, labels, comments, and attachments.
- **Agents have narrow jobs.** Intake, analysis, technical debrief, refinement, implementation, review, deployment, QA, client review, release, and rework are separate roles with explicit triggers and exit criteria.
- **Contracts are enforced in code.** A model cannot move a ticket by prose alone. Status transitions require schema-valid handoff payloads; rejections require actionable findings tied to criteria; passed checklist items require evidence.
- **Humans retain the throttle.** Tickets freeze on `needs-human`; manual Jira transitions are honored; production release requires an explicit release window; the whole pipeline can be paused and resumed operationally.

The result should feel less like "an AI that helps with tickets" and more like an always-on delivery operator that advances only when the next stage has enough verified evidence to inherit the work safely.

## Product ambition

Sentinel should become the reference implementation for **governed autonomous software delivery** in teams that need automation without surrendering control, auditability, or deployment safety.

The product is not a generic agent framework. It is an opinionated delivery system with a strong spine:

1. **State lives where the team works:** Jira Server/Data Center is the source of truth; no parallel database becomes the shadow process.
2. **Prompts are operational documents:** the role docs in `docs/` are runtime artifacts, not passive documentation.
3. **Tooling is the guardrail:** the `sentinel/tools.py` layer refuses invalid transitions instead of merely advising agents to behave.
4. **Evidence beats confidence:** handoffs, reviews, QA, deployments, and releases must carry links, attachments, CI runs, scan results, or explicit assumptions.
5. **Autonomy is bounded:** agents can act, but they cannot invent missing project commands, skip required payloads, ignore rework limits, deploy to production without a window, or override humans.

## Target users

Sentinel is for teams that already run a ticket-based delivery process and want AI to operate inside it rather than outside it.

Primary users:

- **Engineering leads / CTOs** who want faster throughput but need review, security, and release control to remain explicit.
- **Product owners** who want tickets refined into clear business and technical requirements without losing traceability back to client intent.
- **Delivery managers** who want WIP limits, stuck-ticket detection, rework loops, and escalation points enforced mechanically.
- **Regulated or client-facing teams** that need evidence trails for decisions, reviews, deployments, and release sign-off.

Early adopters are likely small-to-mid-sized software teams with enough process maturity to value gates, but enough delivery pressure that manual handoffs and review loops are a bottleneck.

## The promise to users

Sentinel should make three promises:

1. **Every ticket has a next responsible actor.** If an agent can move it forward, it does. If not, the ticket is frozen with a precise human decision needed.
2. **Every handoff is reconstructable.** A downstream role can see what changed, why it passed, what evidence supports it, and which assumptions remain.
3. **Every release is deliberate.** Work reaches production only after client acceptance, artifact identity checks, final security scanning, verification, and a human-opened release window.

## What Sentinel should be excellent at

### 1. Operating the board

The Orchestrator is Sentinel's traffic control layer. It should reliably answer: "What can move now, what must wait, and what needs a human?"

Core capabilities:

- webhook-triggered dispatch with a full sweep safety net;
- WIP-limit enforcement;
- lease claim, heartbeat, reclaim, and retry handling;
- rework loop-breaking;
- waiting-marker wakeups;
- invalid-handoff detection;
- global pause and health visibility.

### 2. Preserving intent across handoffs

The pipeline should prevent intent loss as work moves through roles. Business value, acceptance criteria, technical approach, security checklist, estimates, review findings, deployment evidence, QA evidence, client feedback, and release notes should form a continuous chain rather than disconnected comments.

### 3. Making review meaningful

The review stages must stay independent. Implementers implement; reviewers verify. Review outputs should be binary, evidence-backed, and actionable. Minor issues can become backlog follow-ups, but major and blocker findings must route through Rework with a precise fix brief.

### 4. Shipping safely

Deployment and release automation should prefer escalation over guessing. Project-specific commands in `config/pipeline.yml` are the trust boundary: if clone, test, deploy, smoke, or rollback commands are missing, shell-enabled roles stop and ask a human instead of inventing production behavior.

### 5. Remaining observable

Operators need to know whether Sentinel is healthy, paused, degraded, working, waiting, or escalating. Health endpoints, audit JSONL, Jira comments, labels, and optional outbound alerts should make the system inspectable without reading container logs first.

## Strategic pillars

### Pillar 1 — Contract-first autonomy

Sentinel should expand agent autonomy only when the contract around that autonomy is enforceable. New capabilities should define:

- the input state they consume;
- the allowed tools and side effects;
- the output payload schema;
- validation rules;
- the escalation path when validation fails.

### Pillar 2 — Jira-native adoption

The fastest adoption path is to require as little new infrastructure and behavior change as possible. Jira remains the board, comments remain the discussion surface, labels remain the levers, and issue properties hold machine state without demanding custom-field administration.

### Pillar 3 — Human-controlled risk

Sentinel should automate routine progression while leaving irreversible or ambiguous decisions to humans. The existing controls — `activate`, `needs-human`, `deploy-now`, `release-now`, `/pause`, `/resume` — are product features, not limitations.

### Pillar 4 — Model independence

All LLM calls route through LiteLLM. This keeps the system portable across models and allows higher-trust roles, such as Code Reviewer, to run on a separate model or context from Implementer.

### Pillar 5 — Evidence as the user interface

Sentinel's durable output is not just code or ticket status. It is the evidence trail: payloads, linked artifacts, scan summaries, screenshots, CI runs, deployment build IDs, release manifests, and assumptions with verification paths.

## Near-term roadmap themes

### 1. Operational hardening

- richer health diagnostics for queue backlog, stuck leases, and repeated escalations;
- clearer operator runbooks for pause/resume, failed sweeps, invalid handoffs, and Jira/LiteLLM outages;
- stronger audit querying and incident reconstruction flows;
- alert routing that can distinguish urgent human decisions from routine queue state.

### 2. Project onboarding

- guided setup for `config/pipeline.yml` project commands;
- doctor checks that validate not only connectivity and statuses, but whether shell-enabled roles have usable clone/test/deploy/smoke/rollback contracts;
- examples for common project types and deployment patterns;
- safer dry-run modes for first-time adoption.

### 3. Evidence and review depth

- standardized attachment bundles for scans, screenshots, QA traces, release manifests, and rollback evidence;
- stronger comparison between AC, implementation diff, test coverage, and security checklist;
- explicit backlog-ticket creation for accepted minor findings.

### 4. Governance and analytics

- throughput, wait-time, rework, escalation, and release metrics derived from Jira and audit events;
- role-by-role quality signals, especially where tickets bounce or assumptions repeatedly fail;
- reporting that helps humans improve the workflow, not just watch the agents.

### 5. Extensibility without framework sprawl

- documented patterns for adding roles, tools, and payloads while preserving contract enforcement;
- adapter seams for additional issue trackers only after the Jira-native loop is mature;
- per-project policy packs for security baselines, review standards, and release rules.

## Non-goals

Sentinel should not become:

- a generic chatbot interface for Jira;
- a replacement for human product authority or release accountability;
- a hidden database that competes with the ticket board;
- an agent free-for-all where models can call arbitrary tools without contracts;
- a system that optimizes velocity by weakening review, QA, or release gates;
- a cloud-only product that abandons self-hosted teams and Jira Server/Data Center users before the core workflow is proven.

## Success measures

Sentinel is succeeding when:

- tickets no longer sit silently in agent-owned statuses;
- handoffs can be audited without reconstructing context from chat history;
- review findings are specific, criterion-linked, and routed cleanly through Rework;
- production releases are traceable from client acceptance to build ID and verification evidence;
- humans intervene less often, but with clearer decisions when they do;
- failures degrade safely: pause, escalate, retry once, or wait — never silently guess.

## Design principles for future contributors

1. **Do not add autonomy without a validator.** If a role can do something consequential, encode the acceptance rules in tools or tests.
2. **Do not bypass Jira as source of truth.** Prefer Jira properties, labels, comments, and attachments unless there is a clear operational reason not to.
3. **Do not make prompts the only safety boundary.** Prompts guide behavior; tools enforce it.
4. **Do not hide assumptions.** Every assumption should have an owner and a verification path.
5. **Do not optimize for happy paths only.** Rework, outages, invalid handoffs, missing commands, stale leases, and human overrides are first-class flows.
6. **Do not treat production as another status transition.** Release is a human-throttled operational event with rollback and verification duties.

## The north star

A team should be able to point Sentinel at a Jira project and say:

> "Move everything forward that is safe to move. Stop exactly where human judgment is required. Leave enough evidence that the next person or agent can trust, verify, or challenge every step."

That is the project vision: autonomous delivery, governed by contracts, observable through Jira, and accountable to humans.
