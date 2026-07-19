---
name: issue-autopilot
description: Set up a restart-surviving scheduled Routine that, on each firing, first reworks unaddressed review/CI feedback on its own open PRs and then — only if none is pending — picks the highest-priority open GitHub issue, validates readiness, and either posts a technical debrief or implements it and opens a PR for review. Use when the user asks for a recurring / scheduled issue-triage-and-implementation routine, an "issue autopilot", or a routine that survives sandbox restarts and drives the backlog by priority while keeping its own PRs moving.
---

# Issue autopilot routine

Creates a scheduled Routine (via the claude-code-remote MCP server) that on each firing does
**one unit of work**, in this priority order:

1. **Rework its own open PRs first.** If a PR the routine previously opened (`claude/issue-<NN>-*`)
   has **new, unaddressed feedback** — a CHANGES_REQUESTED review, review comments, PR comments, or
   a failing CI check newer than the PR's head commit — it checks out that branch, fixes it, runs
   the tests, pushes, and replies. Then it stops for that run. Open PRs are not "done"; leaving
   review feedback unaddressed while starting new issues is the failure this step prevents.
2. **Otherwise, advance the backlog.** With no PR feedback pending, it selects the
   **highest-priority open issue** not already in progress, **validates readiness**, and either:
   - **Not ready →** posts a technical **debrief** comment (assessment, gaps, plan, questions) and stops.
   - **Ready →** implements it on a fresh branch, runs the tests, and opens a **PR for review**
     that closes the issue. It never merges.

Across firings the loop keeps its own PRs moving **and** drains the backlog by priority (an issue
with an open PR or feature branch is treated as in progress and skipped in step 2).

The Routine survives sandbox/container restarts because triggers are stored server-side and the
self-bound session is rehydrated in its environment on fire.

## Arguments

`/issue-autopilot [owner/repo] [cron]`
- `owner/repo` — defaults to the origin remote of the current repo.
- `cron` — 5-field cron expression, defaults to hourly `0 * * * *`. Hourly is the minimum interval.

## Critical constraints (verified failures — do not skip)

1. **Self-bind the trigger; never use `create_new_session_on_fire: true`.**
   Fresh sessions spawned by a trigger created from inside a session get **no MCP connectors**:
   no GitHub tools, so every run fails silently. Omit both `create_new_session_on_fire` and
   `persistent_session_id` so the Routine fires into the **current session**, which keeps its
   GitHub MCP tools when rehydrated after a restart.
2. **Remote sessions have no `gh` CLI, and direct `api.github.com` is blocked** by the outbound
   proxy (returns 403). GitHub access is exclusively: `mcp__github__*` tools + `git` through the
   environment's local proxy remote. Do not write routine prompts that assume `gh` or `curl`.
3. If the user wants **isolated fresh sessions per run**, the Routine must instead be created from
   the **claude.ai Routines UI** (connectors can be attached there). Give them the prompt template
   below to paste; it cannot be done from inside a session.
4. The trigger tools (`create_trigger`, `fire_trigger`, `update_trigger`, `delete_trigger`,
   `list_triggers`, `send_later`) live on the claude-code-remote MCP server — its name may appear
   as a UUID. Load them via ToolSearch (query `+trigger create`) if not already available.

## Priority model (this repo)

- Issues carry a `priority:NN` label (`priority:01` … `priority:08`) and a `[PN]` title prefix.
  **Lower number = higher priority; `priority:01` (P1) is the highest.** Pick the lowest number.
- `ai-agent-ready` marks an issue as intended for autonomous work. Treat its **absence** as a
  readiness gap (debrief, don't develop).
- Tiebreak equal priority by **oldest issue number first**.

## Readiness validation (the gate between debrief and develop)

An issue is **ready to develop** only if ALL hold; otherwise it is a **debrief** target:
- state is OPEN and it is not already in progress — **no open PR references it** and **no remote
  branch `claude/issue-<NN>-*` exists**;
- it carries `ai-agent-ready` and is **not** blocked (`blocked`, `needs-human`,
  `needs-clarification`, `question`, or an open unresolved dependency/parent);
- it has concrete **acceptance criteria** and a bounded scope (not "epic"/"discussion");
- the "how" is unambiguous enough to implement without inventing product decisions.

## What counts as "unaddressed" PR feedback (step 1)

Feedback on one of the routine's own open PRs is **unaddressed** when it is **newer than the PR's
head commit** and not already answered by a later routine commit/reply:
- a review whose state is `CHANGES_REQUESTED` (or its review-comment threads still `is_resolved: false`);
- issue-style PR comments requesting changes, posted after the head commit;
- a failing/errored CI check (`get_check_runs` / `get_status`) on the head commit.
Once the routine pushes a fix commit and replies, that feedback is "addressed" (its timestamp is now
older than the new head), so the next run won't re-handle it.

## Procedure

1. Resolve `OWNER/REPO` (from `git remote -v`, else ask), the local clone path, and the project's
   test command (check CLAUDE.md / CI workflow). For **this** repo the test command is:
   `UV_HTTP_TIMEOUT=180 UV_CACHE_DIR=/opt/data/.cache/uv-sentinel uv run --with pytest --with-requirements requirements.txt pytest tests -q`
   (the `UV_HTTP_TIMEOUT` bump avoids proxy fetch timeouts; CI itself runs plain `pytest tests -q`).
2. Call `create_trigger` with:
   - `name`: `"<repo> issue triage & implement loop"`
   - `cron_expression`: the chosen schedule
   - `prompt`: the template below with placeholders filled
   - no targeting fields (self-bind — see constraint 1)
3. Report to the user: trigger id, schedule, `next_run_at`, the PR-feedback-first + debrief-vs-develop
   behavior, and that a run which only finds not-ready issues produces a debrief comment (no PR).

## Prompt template

```
Hourly routine run — {REPO} issue triage & implement loop. Work on {OWNER_REPO} using the GitHub
MCP tools (mcp__github__*, load via ToolSearch if needed) and the local git clone at {CLONE_PATH}.
Do NOT use the gh CLI or curl api.github.com (blocked); use mcp__github__* + git via the origin
proxy remote. Do exactly ONE unit of work per run: either rework one of your own open PRs (STEP 1)
OR advance one issue (STEP 2–4). Follow this decision procedure exactly.

STEP 0 — Setup:
- In the clone, `git fetch origin` (it may be stale). Determine the default branch from origin/HEAD;
  cut and target branches against THAT branch, never assume main.
- Read _INDEX.md and README.md for architecture, invariants, and how to run/test. Also read
  docs/PROJECT_VISION.md for the project north star; it lives on the `main` branch and may be absent
  from the default branch you cut from — if it is not in your working tree, read it with
  `git show origin/main:docs/PROJECT_VISION.md`.

STEP 1 — Rework your OWN open PRs first (they take priority over new issues):
- List OPEN PRs whose head branch matches claude/issue-<NN>-* (PRs this routine opened).
- For each (highest-priority issue first), look for UNADDRESSED feedback = newer than the PR's head
  commit and not already answered by a later commit/reply: a CHANGES_REQUESTED review or unresolved
  review-comment threads (get_reviews / get_review_comments), PR comments requesting changes
  (get_comments), or a failing CI check on the head commit (get_check_runs / get_status).
- If such feedback exists on a PR: check out that PR branch locally (git fetch + checkout), rework
  the code to address every point, run the test suite ({TEST_COMMAND}) until green, commit with a
  clear message, and push to the SAME PR branch (retry network failures up to 4 times, backoff
  2s/4s/8s/16s; never force-push). Then reply: post a PR comment summarizing what changed per point,
  reply on each review thread, and resolve threads you have fixed. Then STOP for this run.
- If ambiguous or the fix needs a product/architecture decision, reply asking for clarification
  instead of guessing, and STOP.
- If no own open PR has unaddressed feedback, continue to STEP 2.

STEP 2 — Select the highest-priority open issue not already in progress:
- List OPEN issues. Rank by the priority:NN label (lower number = higher priority; priority:01 is
  highest), tiebreak by oldest issue number. Skip any issue that already has an OPEN PR referencing
  it or an existing remote branch claude/issue-<NN>-* (that is in-progress work).
- Take the single highest-priority remaining issue as the TARGET.

STEP 3 — Validate readiness of the TARGET. It is READY only if ALL hold, else it is a DEBRIEF case:
- OPEN and not in progress (already ensured in step 2);
- has label ai-agent-ready and is NOT blocked (blocked / needs-human / needs-clarification /
  question / unresolved dependency or open parent issue);
- has concrete acceptance criteria and bounded scope;
- the implementation approach is unambiguous — no product/architecture decision must be invented.

STEP 4a — If the TARGET is NOT ready → DEBRIEF and STOP:
- Post ONE issue comment titled a technical debrief containing: a readiness verdict (not ready) and
  why; the specific gaps/ambiguities/blockers; affected files and invariants to preserve; a
  proposed implementation plan; and the concrete open questions or label/split actions needed to
  make it ready. Do NOT write code or open a PR. End the run.

STEP 4b — If the TARGET is READY → DEVELOP and open a PR, then STOP:
- Post a short debrief comment on the issue first: the plan you are about to implement.
- Create branch claude/issue-<NN>-<short-slug> from the latest default branch.
- Implement the issue completely: code + tests. Update _INDEX.md, README.md, and role/docs in the
  SAME commit whenever architecture, file layout, tools, schemas, config, or invariants change.
- Preserve the invariants from _INDEX.md: Jira stays the source of truth; tools enforce
  consequential contracts (not just prompts); humans always win (manual transitions honored,
  needs-human freezes, release stays human-throttled); every consequential behavior has tests and a
  safe failure/escalation path.
- Run the test suite: {TEST_COMMAND}. It must pass before you push.
- Commit with a clear message, push to the feature branch with -u origin (retry network failures up
  to 4 times, backoff 2s/4s/8s/16s).
- Open a PR against the default branch: clear title, body covering motivation, implementation, and
  verification (test output), and "Closes #<NN>". Use the repo's PR template if one exists. Leave it
  as a normal PR for review — do NOT merge.
- Post an issue comment linking the PR. End the run.

General rules: exactly ONE unit of work per run; never force-push shared branches; never merge PRs;
never push to the default branch directly. If a "ready" issue or a PR fix turns ambiguous, stop and
ask (issue debrief comment / PR reply) instead of guessing.
```

## Testing a new routine

- `fire_trigger` once. On a self-bound trigger the prompt arrives as a turn in the current session,
  so the run executes visibly right here.
- Verify outcomes **externally**, not by trusting the run's narration: `git ls-remote origin` for
  branch-head movement; `mcp__github__pull_request_read` (`get_commits`, `get_reviews`,
  `get_review_comments`, `get_check_runs`) to confirm a PR was reworked and the feedback replied to;
  `list_pull_requests` for a new PR; `mcp__github__issue_read` (`get_comments`) for a debrief/plan.
- Never `sleep` waiting for a fire. To re-check later, schedule `send_later` check-ins
  (15–30 min) that describe exactly what to verify and what to tell the user in each outcome.
- GitHub API 503s are transient; `git ls-remote` still answers during API outages.

## Managing

- Pause / resume: `update_trigger` with `enabled: false` / `true`.
- Reschedule: `update_trigger` with a new `cron_expression`.
- Delete: `delete_trigger`. Inspect state (`last_fired_at`, `next_run_at`): `list_triggers`.
- `update_trigger` cannot change a Routine's **prompt** — to change the decision procedure, delete
  and recreate the trigger from this template. Keep prompt edits in this file so setup stays reproducible.
