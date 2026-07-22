# 00 — Overview & Shared Conventions

Every role goal document in this set references the schemas and protocols defined here. An agent instance is loaded with **this document + `00a-operating-manual.md` + its own role document** as its operating instructions — in that order. The role document defines *what* to produce; the operating manual defines *how to think* while producing it. Neither is optional.

## The pipeline

| # | Role | Consumes status | Produces status (success) |
|---|------|-----------------|---------------------------|
| 01 | Orchestrator | all (loop runner) | — |
| 02 | Intake & Triage | New / On Hold (activated) | Business Requirements |
| 03 | Business Analyst (PO copilot) | Business Requirements | Technical Requirements |
| 04 | Tech Lead / Debrief | Technical Requirements | Technical Refinement |
| 05 | Refinement & Estimation | Technical Refinement | To Do |
| 06 | Sprint Planner | To Do | In Progress (assigned) |
| 07 | Implementer | In Progress | Tech Review |
| 08 | Code Reviewer (Security Gate 1) | Tech Review | Tech Review Accepted |
| 09 | Deployment | Tech Review Accepted / Internal Review Accepted | Internal Review / Client Review |
| 10 | QA (functional + visual, Security Gate 2) | Internal Review | Internal Review Accepted |
| 11 | Client Review Facilitator | Client Review | Client Review Accepted |
| 12 | Release | Client Review Accepted | Done (production) |
| 13 | Rework Router | Rework | In Progress (fix-brief attached) |

Any review role (08, 10, 11) may also produce **Rework**.

## Universal rules (apply to every role)

1. **Work only leased tickets.** Claim before working, release on transition (lease protocol below).
2. **Never transition without a complete handoff payload.** The payload is the contract with the next role.
3. **Never guess on ambiguity.** Missing or contradictory inputs → send the ticket back to the role that owns those inputs, or escalate. An invented assumption is a defect injected into every downstream stage.
4. **Stay in scope.** Each role document has a "Must not" section. Violating it is a failed run even if the output looks useful.
5. **Evidence over assertion.** Every checklist item marked `pass` must carry evidence (link, screenshot, CI run, comment reference).
6. **Humans always win.** A manual status transition by a human is respected, logged, and never reverted by an agent.
7. **Run the self-test before every handoff.** The five questions at the end of `00a-operating-manual.md` are answered — honestly — before any transition, verdict, or escalation leaves an agent. If question 2 (the load-bearing claim) can't be answered, the work isn't done, regardless of checklist state.

## Lease protocol

- Claim: set `assignee` to the agent identity, add label `agent-leased`, post a lease comment with start timestamp.
- Heartbeat: update the lease comment (or custom field) at least every 15 minutes while working.
- Release: on any transition, remove `agent-leased`.
- Reclaim: the Orchestrator reclaims any lease without a heartbeat for 30 minutes.

## Handoff payload (posted as a Jira comment on every transition)

Human-readable summary first, then a fenced YAML block:

```yaml
agent_handoff:
  role: <role-id>              # e.g. 08-code-reviewer
  ticket: <KEY-123>
  timestamp: <ISO 8601>
  verdict: pass | reject | escalate
  from_status: <status>
  to_status: <status>
  checklist:
    - id: <check-id>           # ids defined in each role doc
      result: pass | fail | n/a
      evidence: <url | comment-ref>
  outputs: {}                  # role-specific, defined per role doc
  assumptions:                 # everything used but not verified (manual §5) — empty list means "none", never "didn't track"
    - claim: <what was assumed>
      verify_by: <check> @ <stage/role>
  notes: <free text>
```

## Rejection payload (any role sending a ticket to Rework)

```yaml
rework:
  rejected_from: tech_review | internal_review | client_review
  rework_count: <n>            # incremented by Rework Router, read by all
  findings:
    - id: F-1
      severity: blocker | major | minor
      criterion_ref: AC-3 | SEC-2 | STD-<rule> | VIS-1   # ties every finding to a criterion or standard
      location: <file:line | URL | screen/breakpoint>
      description: <what is wrong>
      required_action: <what "fixed" means>
      evidence: <link/screenshot>
```

Rules: every finding must have a `criterion_ref` — "I don't like it" is not a finding. `minor`-only findings may be accepted with a follow-up ticket instead of rejection (reviewer's judgment, documented).

## Escalation protocol

When a role document says "escalate": add label `needs-human`, post a comment with the reason and the specific decision needed, notify the project channel, release the lease. The ticket freezes until a human acts.

Mandatory escalation triggers (all roles): `rework_count` > 2 · contradictory requirements between PO and tech approach · any action that would touch production outside role 12 · security finding of severity `blocker` that the checklist doesn't cover.

## Security baseline

- Named standard: the **project policy's security baseline** (`config/policy.yml` → `security.baseline`; default **OWASP ASVS Level 2**). The active policy — security, review, QA, and release rules — is summarized in every agent's runtime prompt; follow it over any weaker default here. "Security standards matched" always means: checked against the declared per-ticket checklist, never against vibes.
- Declared per ticket by role 04 as `SEC-*` items; self-checked by 07; statically enforced by 08; dynamically verified by 10; re-scanned by 12.

## Definitions (referenced by role docs)

- **DoR-business** (owned by 03): user story, business value, testable Given/When/Then AC, explicit out-of-scope list, affected roles, priority rationale, PO approval on record.
- **DoR-technical** (owned by 05): technical approach, subtasks, agreed estimate, risk log, drafted test scenarios, no blocking dependency, security checklist attached.
- **DoD** (verified cumulatively by 08 + 10): all AC pass with evidence, CI green, security gates green, visual QA within tolerance, docs/release notes updated.

## Ticket metadata (custom fields)

`agent_lease` · `rework_count` · `rejected_from` · `security_checklist` · `open_questions` · `evidence_links` · `deployed_build` (per environment)
