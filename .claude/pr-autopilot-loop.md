# PR autopilot loop — session instructions

This is the runtime instruction for the recurring **PR-feedback / feature loop** on this
repo. It is the exact decision procedure the scheduled Routine fires each run. Use it to
drive a session by hand, or as the prompt when (re)creating the Routine.

**How to use in a new session:** paste the block below verbatim, or say
*"follow `.claude/pr-autopilot-loop.md`"*. One pass = one run: handle feedback on an open
PR, or (only when nothing is open) ship one feature — then stop.

Related: [`.claude/skills/pr-autopilot/SKILL.md`](skills/pr-autopilot/SKILL.md) documents how
to set this up as a restart-surviving scheduled Routine (and the constraints that make it
work in remote sessions — self-bind the trigger, no `gh` CLI, GitHub via `mcp__github__*`).

---

## The instruction (verbatim trigger prompt)

```
Hourly routine run — Sentinel-roles PR feedback & feature loop. Work on
vectorpeak-labs/sentinel-roles using the GitHub MCP tools (mcp__github__*, load via
ToolSearch if needed) and the local git clone (git fetch origin first; the clone may be
stale). Follow this decision procedure exactly:

STEP 1 — Check open PRs for new feedback:
- List all OPEN pull requests. For each, look for NEW, UNADDRESSED feedback: issue
  comments, review comments, reviews, or CI failures that are newer than the last commit
  on the PR branch and not already answered by a Claude reply comment.
- If such feedback exists: fetch and check out the PR branch locally, rework the code per
  the feedback, run the test suite (pytest tests -q), commit with a clear message, push to
  the SAME PR branch (retry on network failure up to 4 times, backoff 2s/4s/8s/16s), and
  post a PR comment summarizing what changed in response to which feedback. Then STOP for
  this run.
- If open PRs exist but none have new unaddressed feedback: do nothing — end the turn
  quietly without messaging the user.

STEP 2 — Only if there are NO open PRs at all:
- Identify ONE critical missing feature (read _INDEX.md per CLAUDE.md; also check
  TODO/FIXME markers and open GitHub issues). Verify via open AND closed PRs and existing
  branches that it isn't already implemented or proposed.
- Implement it completely on a new branch claude/<short-feature-slug> cut from the latest
  default branch: code + tests, update _INDEX.md in the same commit if
  architecture/files/tools/schemas/config/invariants change, run tests/linters, commit,
  push (same retry policy), and open a fresh PR with a clear title and a body explaining
  motivation, implementation, and verification. Use the repo's PR template if one exists.

General rules: never force-push shared branches, never merge PRs, never push to the default
branch. If feedback is ambiguous, ask for clarification via a PR comment instead of
guessing.
```
