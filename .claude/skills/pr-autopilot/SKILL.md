---
name: pr-autopilot
description: Set up a restart-surviving scheduled Routine that reworks new feedback on open PRs (push fixes + reply via PR comments) or, when no PRs are open, ships one critical missing feature as a fresh PR. Use when the user asks for a recurring / scheduled PR-feedback routine, a "PR autopilot", or a routine that survives sandbox restarts.
---

# PR autopilot routine

Creates a scheduled Routine (via the claude-code-remote MCP server) that on each firing either
(1) reworks new, unaddressed feedback on open PRs and replies with a PR comment, or
(2) if no PRs are open, identifies one critical missing feature, implements it, and opens a fresh PR.
The Routine survives sandbox/container restarts because triggers are stored server-side and
self-bound sessions are rehydrated in their environment on fire.

## Arguments

`/pr-autopilot [owner/repo] [cron]`
- `owner/repo` — defaults to the origin remote of the current repo.
- `cron` — 5-field cron expression, defaults to hourly `0 * * * *`. Hourly is the minimum interval.

## Critical constraints (verified failures — do not skip)

1. **Self-bind the trigger; never use `create_new_session_on_fire: true`.**
   Fresh sessions spawned by a trigger created from inside a session get **no MCP connectors**:
   no GitHub tools, so every run fails silently (observed: 7 firings, zero GitHub activity).
   Omit both `create_new_session_on_fire` and `persistent_session_id` so the Routine fires into
   the **current session**, which keeps its GitHub MCP tools when rehydrated after a restart.
2. **Remote sessions have no `gh` CLI, and direct `api.github.com` is blocked** by the outbound
   proxy (returns 403). GitHub access is exclusively: `mcp__github__*` tools + `git` through the
   environment's local proxy remote. Do not write routine prompts that assume `gh` or `curl`.
3. If the user wants **isolated fresh sessions per run**, the Routine must instead be created from
   the **claude.ai Routines UI** (connectors can be attached there). Give them the prompt template
   below to paste; it cannot be done from inside a session.
4. The trigger tools (`create_trigger`, `fire_trigger`, `update_trigger`, `delete_trigger`,
   `list_triggers`, `send_later`) live on the claude-code-remote MCP server — its name may appear
   as a UUID. Load them via ToolSearch (query `+trigger create`) if not already available.

## Procedure

1. Resolve `OWNER/REPO` (from `git remote -v`, else ask), the local clone path, and the project's
   test command (check CLAUDE.md / CI workflow; for this repo it is `pytest tests -q`).
2. Call `create_trigger` with:
   - `name`: `"<repo> PR feedback & feature loop"`
   - `cron_expression`: the chosen schedule
   - `prompt`: the template below with placeholders filled
   - no targeting fields (self-bind — see constraint 1)
3. Report to the user: trigger id, schedule, `next_run_at`, the two-branch behavior, and that
   quiet runs (open PRs but no new feedback) intentionally produce no output.

## Prompt template

```
Hourly routine run — {REPO} PR feedback & feature loop. Work on {OWNER_REPO} using the GitHub MCP
tools (mcp__github__*, load via ToolSearch if needed) and the local git clone at {CLONE_PATH}
(git fetch origin first; the clone may be stale). Follow this decision procedure exactly:

STEP 1 — Check open PRs for new feedback:
- List all OPEN pull requests. For each, look for NEW, UNADDRESSED feedback: issue comments,
  review comments, reviews, or CI failures that are newer than the last commit on the PR branch
  and not already answered by a Claude reply comment.
- If such feedback exists: fetch and check out the PR branch locally, rework the code per the
  feedback, run the test suite ({TEST_COMMAND}), commit with a clear message, push to the SAME
  PR branch (retry on network failure up to 4 times, backoff 2s/4s/8s/16s), and post a PR comment
  summarizing what changed in response to which feedback. Then STOP for this run.
- If open PRs exist but none have new unaddressed feedback: do nothing — end the turn quietly
  without messaging the user.

STEP 2 — Only if there are NO open PRs at all:
- Identify ONE critical missing feature (read the project's index/docs per CLAUDE.md; also check
  TODO/FIXME markers and open GitHub issues). Verify via open AND closed PRs and existing branches
  that it isn't already implemented or proposed.
- Implement it completely on a new branch claude/<short-feature-slug> cut from the latest default
  branch: code + tests, update project index docs in the same commit if architecture/files/tools/
  schemas/config/invariants change, run tests/linters, commit, push (same retry policy), and open
  a fresh PR with a clear title and a body explaining motivation, implementation, and verification.
  Use the repo's PR template if one exists.

General rules: never force-push shared branches, never merge PRs, never push to the default
branch. If feedback is ambiguous, ask for clarification via a PR comment instead of guessing.
```

## Testing a new routine

- `fire_trigger` once. On a self-bound trigger the prompt arrives as a turn in the current
  session, so the run executes visibly right here.
- Verify outcomes **externally**, not by trusting the run's narration: `git ls-remote origin`
  for branch-head movement; `mcp__github__pull_request_read` (`get`, `get_comments`) for replies.
- Never `sleep` waiting for a fire. To re-check later, schedule `send_later` check-ins
  (15–30 min) that describe exactly what to verify and what to tell the user in each outcome.
- GitHub API 503s are transient; `git ls-remote` still answers during API outages.

## Managing

- Pause / resume: `update_trigger` with `enabled: false` / `true`.
- Reschedule: `update_trigger` with a new `cron_expression`.
- Delete: `delete_trigger`. Inspect state (`last_fired_at`, `next_run_at`): `list_triggers`.
- A paused or deleted trigger can always be recreated from this template — keep changes to the
  prompt template in this file so the setup stays reproducible.
