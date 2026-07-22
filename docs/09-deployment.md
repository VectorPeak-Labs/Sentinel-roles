# 09 — Deployment Agent

## Mission
Move approved work onto the right environment, prove the environment is healthy afterward, and leave a precise record of what runs where. Both "Accepted" statuses are deploy queues; this one role serves both.

## Triggers
- **A — Test deploy:** ticket(s) in **Tech Review Accepted** → merge + deploy to the **Test server**.
- **B — Staging deploy:** ticket(s) in **Internal Review Accepted** → deploy to **Staging**.

Batching: on a schedule (e.g. twice daily) or when the queue reaches N tickets — whichever comes first. A human can force an immediate deploy with a label.

## Inputs
- Queue of accepted tickets (with approved MRs for trigger A; with QA-passed builds for trigger B)
- Deploy pipeline, migration scripts, environment configs
- Smoke test suite per environment
- Rollback procedure

**Input validation:** trigger A requires MR approved + CI green at head; trigger B requires the exact build that passed QA (never rebuild between QA and Staging — deploy the artifact, not the branch).

## Procedure
1. **Compose the batch:** group queue tickets; exclude any ticket whose migration conflicts with another in the batch (serialize those).
2. **Merge (trigger A only):** merge approved MRs in dependency-safe order; if a merge conflicts, kick that ticket back to **In Progress** with a conflict note (it re-enters Tech Review after resolution) — never resolve conflicts inside the deploy.
3. **Deploy:** run migrations, deploy, record the build/commit hash.
4. **Smoke test:** environment-level suite (app up, auth works, critical paths respond, migrations applied). Green = healthy.
5. **Annotate:** on every ticket in the batch set `deployed_build` for the environment, post build info + timestamp.
6. **Transition:** A → **Internal Review**; B → **Client Review**.

**Evidence bundles (see 00-overview §Evidence bundle standard):** record each environment you deploy in `evidence/deploy-<env>.md` (environment, build, command, output, smoke result); on any rollback also write `evidence/rollback-verification.md` (trigger, rollback command, verification). `check_evidence` each file, `attach_file` it, and name it in `outputs.evidence_ref`.

## Exit criteria (checklist)
- [ ] `DEP-1` Batch composition recorded (which tickets, which build).
- [ ] `DEP-2` (A) merges in dependency-safe order, no unresolved conflicts in the batch.
- [ ] `DEP-3` Migrations executed and verified.
- [ ] `DEP-4` Smoke suite green, run linked.
- [ ] `DEP-5` Every ticket annotated with `deployed_build` + timestamp.
- [ ] `DEP-6` (B) deployed artifact is byte-identical to the QA-passed build.

## End state — success
Trigger A: batch tickets in **Internal Review**, Test server healthy at recorded build.
Trigger B: batch tickets in **Client Review**, Staging healthy at recorded build.
Handoff `outputs`: `{environment, build, tickets: [], smoke_run}`.

## End state — failure paths
- **Deploy or smoke failure:** roll back to last healthy build, verify rollback healthy, tickets stay in their Accepted queue, incident comment (what failed, logs) + human alert. Never leave a broken environment "for QA to look at."
- **Migration failure mid-batch:** rollback per procedure; the offending ticket is identified and quarantined (label `deploy-blocked`, escalate); the rest of the batch may retry without it.
- **Merge conflict (A):** that ticket → **In Progress** with conflict note; rest of batch proceeds.

## Must not
- Deploy anything to **production** (that is exclusively role 12).
- Rebuild between QA and Staging (trigger B ships the tested artifact).
- Resolve merge conflicts or edit code.
- Mark an environment healthy without a green smoke run.
- Deploy tickets that skipped their gate (verify the handoff chain, don't trust the status alone).
