"""Sentinel — an agent loop that drives a Jira-based development workflow.

Each pipeline role (docs/01–13) runs as an LLM agent loaded with the shared
conventions (docs/00), the operating manual (docs/00a) and its own role
document. Jira is the single source of truth: leases, rework counters and
handoff payloads live on the tickets themselves.
"""

__version__ = "0.1.0"
