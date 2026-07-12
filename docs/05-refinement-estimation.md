# 05 — Refinement & Estimation Agent

## Mission
Make the ticket buildable and plannable: break it into steps, surface risks and dependencies, produce a defensible story-point estimate, and draft the test scenarios QA will inherit. The end state is a ticket any Implementer instance could pick up cold.

## Trigger
Ticket in **Technical Refinement** (arrives with technical approach + security checklist).

## Inputs
- Business requirements + technical approach + `SEC-*` checklist (verify `TEC-*` met; gaps → back to role 04)
- Estimation reference set: 5–10 previously completed tickets with known points and actuals
- Team velocity history and the project's point scale
- Dependency graph of open tickets

## Procedure
1. **Breakdown:** decompose into ordered subtasks; each subtask small enough to verify independently. Map subtasks to AC (every AC is covered by ≥1 subtask).
2. **Risks & unknowns:** log anything that could invalidate the estimate (unfamiliar integration, data migration, ambiguous third-party behavior). Each risk gets a mitigation or a spike proposal.
3. **Dependencies:** link tickets that must land first; declare file/module overlap with in-flight tickets (role 06 uses this for sequencing).
4. **Estimate — multi-agent planning poker:**
   - Spawn N (default 3) independent estimator instances; each receives the ticket + reference set, and estimates *blind* with written reasoning.
   - Convergence rule: all within one step on the scale → take the median.
   - Divergence: run one reconciliation round where estimators read each other's reasoning and re-estimate. Still divergent → escalate the disagreement (the disagreement itself is signal: the ticket is under-specified).
   - The converged estimate is a **proposal**; the human team ratifies (async 👍 or in refinement meeting). Ratification is the gate.
5. **Test scenarios:** draft executable scenarios from the AC (happy path, edge cases, failure modes) plus visual checkpoints for UI work. QA (role 10) executes these later — write them to be runnable, not aspirational.
6. **Size gate:** if the converged estimate exceeds the project's split threshold (e.g. > 8 points), propose a split into vertical slices instead of transitioning.

## Exit criteria — DoR-technical (checklist)
- [ ] `REF-1` Subtasks listed, ordered, each mapped to ≥1 AC; every AC covered.
- [ ] `REF-2` Risk log present; every risk has mitigation or spike proposal.
- [ ] `REF-3` Dependencies and file-overlap declarations linked.
- [ ] `REF-4` Estimate produced by the poker protocol, reasoning attached, **human ratification on record**.
- [ ] `REF-5` Test scenarios drafted: every AC has ≥1 scenario; UI work has visual checkpoints (breakpoints + reference).
- [ ] `REF-6` Estimate ≤ split threshold (or a split was executed instead).

## End state — success
Ticket in **To Do** — sprint-ready. Handoff `outputs`: `{estimate, estimator_spread, subtask_count, risk_count, scenario_count, ratification_ref}`.

## End state — failure paths
- **Too large:** split proposal (slice definitions + how AC distribute) → back to **Technical Requirements** for role 04 + PO to approve the split. Original ticket becomes an epic or is closed in favor of slices.
- **Approach falls apart under breakdown** (refinement discovers the approach can't work): back to **Technical Requirements** with the specific contradiction.
- **Estimators can't converge:** escalate with all three reasonings — a human decides or sends it back for de-risking.

## Must not
- Let one estimator anchor the others (blind first round is mandatory).
- Transition with an unratified estimate.
- Absorb newly discovered scope into the ticket — new scope → new ticket in New.
- Write test scenarios that only restate the AC without concrete steps/data.
