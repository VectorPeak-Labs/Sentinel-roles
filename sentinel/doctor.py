"""Pre-flight checks: `python -m sentinel.doctor` (or `docker compose run --rm sentinel doctor`).

Verifies Jira connectivity + project + workflow statuses, LiteLLM reachability,
and that every role document referenced by the config exists.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

from .config import load_settings
from .jira import JiraClient, JiraError
from .llm import LLM

OK, FAIL, WARN = "  [ok]  ", "  [FAIL]", "  [warn]"


async def main() -> int:
    failures = 0
    try:
        settings = load_settings()
        print(f"{OK} configuration loaded ({len(settings.roles)} roles)")
    except Exception as e:
        print(f"{FAIL} configuration: {e}")
        return 1

    for role in settings.roles.values():
        doc = Path(role.doc)
        if doc.exists():
            print(f"{OK} {role.role_id}: {role.doc}")
        else:
            print(f"{FAIL} {role.role_id}: missing role document {role.doc}")
            failures += 1
    for shared in ("00-overview-and-conventions.md", "00a-operating-manual.md"):
        if not (settings.docs_dir / shared).exists():
            print(f"{FAIL} missing shared document {settings.docs_dir / shared}")
            failures += 1

    jira = JiraClient(settings.jira_base_url, settings.jira_pat)
    try:
        me = await jira.myself()
        print(f"{OK} Jira reachable at {settings.jira_base_url} as "
              f"'{me.get('name')}' ({me.get('displayName')})")
        statuses = (await jira._request("GET", f"/project/{settings.jira_project}/statuses")).json()
        known = {s["name"].lower() for issue_type in statuses for s in issue_type.get("statuses", [])}
        print(f"{OK} project {settings.jira_project} found "
              f"({len(known)} workflow status(es))")
        for status in settings.agent_statuses:
            if status.lower() in known:
                print(f"{OK} workflow status '{status}'")
            else:
                print(f"{FAIL} workflow status '{status}' not found in project "
                      f"{settings.jira_project} — fix the Jira workflow or edit "
                      f"config/pipeline.yml")
                failures += 1
    except (JiraError, httpx.HTTPError) as e:
        print(f"{FAIL} Jira: {e}")
        failures += 1
    finally:
        await jira.close()

    llm = LLM(settings.litellm_base_url, settings.litellm_api_key, settings.default_model)
    try:
        msg = await llm.chat([{"role": "user", "content": "Reply with the single word: ok"}])
        print(f"{OK} LiteLLM reachable at {settings.litellm_base_url}, model "
              f"'{settings.default_model}' answered: {(msg.content or '').strip()[:40]!r}")
    except Exception as e:
        print(f"{FAIL} LiteLLM ({settings.litellm_base_url}, model "
              f"'{settings.default_model}'): {e}")
        failures += 1
    finally:
        await llm.close()

    reviewer = settings.roles.get("08-code-reviewer")
    if reviewer and not reviewer.model:
        print(f"{WARN} SENTINEL_REVIEWER_MODEL is not set — the Code Reviewer will run on "
              f"the default model in a separate context (role 08 recommends a different model)")

    if not any(settings.commands.values()):
        print(f"{WARN} no project commands configured in config/pipeline.yml — "
              f"implementer/deploy/release agents will escalate when they need them")

    print("\nall checks passed" if failures == 0 else f"\n{failures} check(s) failed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
