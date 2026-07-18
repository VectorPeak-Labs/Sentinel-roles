---
name: issue-autopilot
description: Set up a restart-surviving scheduled Routine that, on each firing, picks the highest-priority open GitHub issue, validates whether it is ready to be worked, then either posts a technical debrief (when it is not ready) or implements it and opens a PR for review (when it is). Use when the user asks for a recurring / scheduled issue-triage-and-implementation routine, an "issue autopilot", or a routine that survives sandbox restarts and works the backlog by priority.
---

# Issue autopilot routine

Creates a scheduled Routine (via the claude-code-remote MCP server) that on each firing:

1. Selects the **highest-priority open issue** that is not already in progress.
2. **Validates readiness** — is the issue actually workable, or under-specified / blocked?
3. Then branches:
   - **Not ready →** posts a technical **debrief** comment (readiness assessment, gaps, plan,
     open questions) and stops. It does not guess.
   - **Ready →** implements it on a fresh branch, runs the tests, and opens a **PR for review**
     that closes the issue. It never merges.

One firing works **one** issue. Across firings the loop drains the backlog by priority, because
an issue that already has an open PR or feature branch is treated as in progress and skipped.

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

## Readiness validation (step 2 — the gate between debrief and develop)

An issue is **ready to develop** only if ALL hold; otherwise it is a **debrief** target:
- state is OPEN and it is not already in progress — **no open PR references it** and **no remote
  branch `claude/issue-<NN>-*` exists**;
- it carries `ai-agent-ready` and is **not** blocked (`blocked`, `needs-human`,
  `needs-clarification`, `question`, or an open unresolved dependency/parent);
- it has concrete **acceptance criteria** and a bounded scope (not "epic"/"discussion");
- the "how" is unambiguous enough to implement without inventing product decisions.

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
3. Report to the user: trigger id, schedule, `next_run_at`, the debrief-vs-develop behavior, and
   that a run which only finds not-ready issues produces a debrief comment (no PR), not silence.

## Prompt template

```
Hourly routine run — {REPO} issue triage & implement loop. Work on {OWNER_REPO} using the GitHub
MCP tools (mcp__github__*, load via ToolSearch if needed) and the local git clone at {CLONE_PATH}.
Do NOT use the gh CLI or curl api.github.com (blocked); use mcp__github__* + git via the origin
proxy remote. Follow this decision procedure exactly and work exactly ONE issue per run.

STEP 0 — Setup:
- In the clone, `git fetch origin` (it may be stale). Determine the default branch from
  origin/HEAD; cut and target branches against THAT branch, never assume main.
- Read _INDEX.md and README.md for architecture, invariants, and how to run/test. Also read
  docs/PROJECT_VISION.md for the project north star; it lives on the `main` branch and may be absent
  from the default branch you cut from — if it is not in your working tree, read it with
  `git show origin/main:docs/PROJECT_VISION.md`.

STEP 1 — Select the highest-priority open issue not already in progress:
- List OPEN issues. Rank by the priority:NN label (lower number = higher priority; priority:01 is
  highest), tiebreak by oldest issue number. Skip any issue that already has an OPEN PR referencing
  it or an existing remote branch claude/issue-<NN>-*  (that is in-progress work).
- Take the single highest-priority remaining issue as the TARGET.

STEP 2 — Validate readiness of the TARGET. It is READY only if ALL hold, else it is a DEBRIEF case:
- OPEN and not in progress (already ensured in step 1);
- has label ai-agent-ready and is NOT blocked (blocked / needs-human / needs-clarification /
  question / unresolved dependency or open parent issue);
- has concrete acceptance criteria and bounded scope;
- the implementation approach is unambiguous — no product/architecture decision must be invented.

STEP 3a — If the TARGET is NOT ready → DEBRIEF and STOP:
- Post ONE issue comment titled a technical debrief containing: a readiness verdict (not ready) and
  why; the specific gaps/ambiguities/blockers; affected files and invariants to preserve; a
  proposed implementation plan; and the concrete open questions or label/split actions needed to
  make it ready. Do NOT write code or open a PR. End the run.

STEP 3b — If the TARGET is READY → DEVELOP and open a PR, then STOP:
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

General rules: work only ONE issue per run; never force-push shared branches; never merge PRs;
never push to the default branch directly. If a "ready" issue turns ambiguous mid-implementation,
stop and switch to a debrief comment with your questions instead of guessing.
```

## Testing a new routine

- `fire_trigger` once. On a self-bound trigger the prompt arrives as a turn in the current session,
  so the run executes visibly right here.
- Verify outcomes **externally**, not by trusting the run's narration: `git ls-remote origin` for
  branch-head movement; `mcp__github__pull_request_read` / `list_pull_requests` for the PR;
  `mcp__github__issue_read` (`get_comments`) for the debrief/plan comment.
- Never `sleep` waiting for a fire. To re-check later, schedule `send_later` check-ins
  (15–30 min) that describe exactly what to verify and what to tell the user in each outcome.
- GitHub API 503s are transient; `git ls-remote` still answers during API outages.

## Managing

- Pause / resume: `update_trigger` with `enabled: false` / `true`.
- Reschedule: `update_trigger` with a new `cron_expression`.
- Delete: `delete_trigger`. Inspect state (`last_fired_at`, `next_run_at`): `list_triggers`.
- A paused or deleted trigger can always be recreated from this template — keep changes to the
  prompt template in this file so the setup stays reproducible.
