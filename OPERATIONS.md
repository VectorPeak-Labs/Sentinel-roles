# Operating Sentinel

How to monitor a running Sentinel, what to alert on, and what to do when an alert
fires. Everything here is built on two unauthenticated read endpoints — `GET /health`
(point-in-time) and `GET /metrics` (Prometheus text format) — plus the auth-guarded
`GET /audit` query endpoint for investigation.

## Scraping

Point a Prometheus scraper at `http://<sentinel-host>:8080/metrics` on a trusted
network (or authenticate at your reverse proxy). All series are prefixed `sentinel_`.
Counters reset on container restart — always alert on `rate()`/`increase()`, not on
absolute values.

## The three circuit breakers

| Breaker | Trigger | Behavior | Resume |
|---|---|---|---|
| Operator pause | `POST /pause` (human) | no new dispatch; in-flight runs drain | **manual** `POST /resume` |
| Token budget | UTC-day tokens ≥ `SENTINEL_LLM_DAILY_TOKEN_BUDGET` | same pause (persisted, alerted) | **manual** — a blown budget means something ran away |
| LLM outage gate | `sentinel_llm_consecutive_failures` ≥ 3 | dispatch suspended; one probe call per sweep | **automatic** on first successful probe |

`GET /health` reports which one is active: `status: paused` + `pause_reason` for the
first two, `llm.gated: true` for the third.

## Recommended alerts

```yaml
groups:
- name: sentinel
  rules:
  # The LLM backend is down (or the API key is bad): agents cannot work.
  # llm_up flips to 0 at 3 consecutive failed calls — the same threshold that
  # flips /health to "degraded" and engages the dispatch gate. Each failure
  # already survived the client's internal retries, so 10 sustained minutes
  # is a real outage, not a blip.
  - alert: SentinelLLMDown
    expr: sentinel_llm_up == 0
    for: 10m
    annotations: {summary: "LiteLLM backend failing — dispatch is gated"}

  # Jira is unreachable (expired PAT, network, Jira restart): sweeps failing.
  - alert: SentinelSweepsFailing
    expr: sentinel_consecutive_sweep_failures >= 2
    for: 15m
    annotations: {summary: "Board sweeps failing — /health is degraded"}

  # Escalations are piling up and nobody is acting on them.
  - alert: SentinelNeedsHumanBacklog
    expr: sentinel_needs_human_tickets > 0
    for: 2h
    annotations: {summary: "Tickets frozen awaiting a human for 2h+"}

  # The pipeline has been frozen for a long time — deliberate or forgotten?
  - alert: SentinelPausedLong
    expr: sentinel_paused == 1
    for: 4h
    annotations: {summary: "Pipeline paused for 4h+ — resume or extend on purpose"}

  # Abnormal token burn — a rework loop or chatty prompt, visible per role.
  - alert: SentinelTokenBurnHigh
    expr: sum(rate(sentinel_llm_prompt_tokens_total[15m])) * 86400
          > 2 * sentinel_llm_daily_token_budget != 0
    annotations: {summary: "Token burn rate would blow 2x the daily budget"}

  # Budget nearly exhausted — act before the breaker freezes the pipeline.
  - alert: SentinelTokenBudgetNearlyExhausted
    expr: sentinel_llm_tokens_today / (sentinel_llm_daily_token_budget != 0) > 0.8
    annotations: {summary: "80% of the daily token budget consumed"}

  # Agents producing invalid handoffs — usually a role-doc or model regression.
  - alert: SentinelInvalidHandoffs
    expr: increase(sentinel_handoff_invalid_total[1h]) > 0
    annotations: {summary: "Agent transitions rejected for invalid handoffs"}

  # Scrape target gone (container dead, port unreachable).
  - alert: SentinelDown
    expr: up{job="sentinel"} == 0
    for: 5m
    annotations: {summary: "Sentinel is not answering /metrics"}
```

Queue-depth trends are also worth watching:
`sentinel_tickets_in_status{status="Rework"}` growing means a systemic quality
problem; a `To Do` backlog the Sprint Planner cannot drain means upstream
starvation or capacity misconfiguration.

## Runbook

**SentinelLLMDown / `llm.gated: true`.** Check `llm.last_error` in `/health` — it is a
sanitized label like `AuthenticationError (HTTP 401)` (bad API key: fix `.env`) or
`APIConnectionError` (backend down/unreachable). Nothing else to do: dispatch is
gated so no retry budgets burn, the orchestrator probes once per sweep, and the
gate lifts itself (with an `llm_recovered` alert) as soon as a probe succeeds. If
you fixed a bad key, `docker compose restart sentinel` picks it up; the next probe
recovers the pipeline.

**Token budget tripped (`pause_reason` mentions the budget).** Find what burned it
before resuming: `GET /metrics` shows per-role/model consumption
(`sentinel_llm_prompt_tokens_total{role,model}`), and
`GET /audit?event=dispatch&limit=200` shows what ran. A rework ping-pong shows up
as repeated dispatches on the same ticket — freeze that ticket (`needs-human`
label) before `POST /resume`, or raise the budget if the spend was legitimate.

**Needs-human backlog.** Each frozen ticket carries a Jira comment explaining why.
`GET /audit?ticket=SENT-42` reconstructs the full pipeline history when the comment
lacks context. Stale escalations re-alert every `SENTINEL_STALE_ESCALATION_HOURS`
until someone acts — silencing the reminder means acting on the ticket (remove the
`needs-human` label to resume it).

**Sweeps failing.** Almost always Jira: expired PAT (regenerate, update `.env`,
restart), Jira restarting (self-heals; transient blips are already retried with
backoff and never surface here), or a broken JQL/status rename (did someone change
the workflow? `docker compose run --rm doctor` validates every pipeline status).

**Invalid handoffs.** An agent tried to transition without a schema-valid payload —
the orchestrator froze the ticket with `handoff-invalid`. A burst across many
tickets after a model or role-doc change means the change regressed payload
discipline: pause, revert, resume, then clear the labels.
