# Sentinel Roles

Goal documents for an AI agent loop that drives a Jira-based development workflow. Each pipeline role is defined as one status transition with explicit triggers, exit criteria, end states, and failure paths.

## Loading contract

An agent instance is loaded with, in order:

1. `00-overview-and-conventions.md` — shared schemas: handoff payload, rejection payload, lease protocol, escalation, DoR/DoD
2. `00a-operating-manual.md` — the reasoning craft layer: eight disciplines + the five-question self-test run before every handoff
3. Its own role document (`01`–`13`)

## Pipeline

Intake → Business Analyst → Tech Lead Debrief → Refinement → Sprint Planner → Implementer → Code Review (Security Gate 1) → Deploy to Test → QA incl. visual (Security Gate 2) → Deploy to Staging → Client Review → Release. Rework Router handles all rejections; the Orchestrator runs the loop.
