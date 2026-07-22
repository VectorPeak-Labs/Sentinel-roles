"""Role prompt regression / evaluation suite: ``python -m sentinel.eval_prompts``.

The role documents in ``docs/`` are loaded verbatim as agent system prompts, so
a change to them can alter behavior without touching a line of Python. This suite
guards the load-bearing instructions.

Two layers, exactly as issue #35 frames them:

- **Deterministic (default, CI-safe, no LLM/secrets):** for each scenario in
  ``evals/role_behaviors.yml`` it builds the role's *assembled* system prompt via
  :func:`sentinel.agent.build_system_prompt` (shared docs + role doc + runtime
  preamble — what the model actually sees) and asserts the rubric's required
  concepts are still present. A miss fails the run and names the role + concept
  that regressed. A fake LLM can't prove a prompt *causes* a behavior (it replays
  scripted tool calls), so the CI-safe layer verifies the *instruction* is
  present; that is the regression signal.
- **Model-backed (opt-in ``--model``):** sends each scenario's system prompt +
  probe to LiteLLM and prints the response for a human to eyeball. Needs the
  LiteLLM env; never run in CI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .agent import build_system_prompt
from .config import Settings, load_settings

DEFAULT_RUBRIC_PATH = "evals/role_behaviors.yml"


@dataclass(frozen=True)
class Rubric:
    id: str
    role: str
    scenario: str
    invariant: str
    must_include: tuple[tuple[str, ...], ...]   # AND of groups; each group is OR of phrases
    probe: str | None = None


@dataclass
class EvalResult:
    id: str
    role: str
    passed: bool
    missing: list[str] = field(default_factory=list)   # human labels of absent concept groups
    scenario: str = ""
    invariant: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "role": self.role, "passed": self.passed,
                "missing": self.missing, "scenario": self.scenario,
                "invariant": self.invariant}


def load_rubrics(path: str | os.PathLike = DEFAULT_RUBRIC_PATH) -> list[Rubric]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    scenarios = raw.get("scenarios") or []
    rubrics: list[Rubric] = []
    for s in scenarios:
        groups = s.get("must_include") or []
        rubrics.append(Rubric(
            id=str(s["id"]), role=str(s["role"]),
            scenario=str(s.get("scenario", "")), invariant=str(s.get("invariant", "")),
            must_include=tuple(tuple(str(a) for a in group) for group in groups),
            probe=s.get("probe")))
    return rubrics


def evaluate_prompt(rubric: Rubric, prompt: str) -> EvalResult:
    """Pure: does `prompt` contain every required concept group (case-insensitive)?"""
    low = prompt.lower()
    missing = [" | ".join(group) for group in rubric.must_include
               if not any(phrase.lower() in low for phrase in group)]
    return EvalResult(rubric.id, rubric.role, not missing, missing,
                      rubric.scenario, rubric.invariant)


def run_deterministic(rubrics: list[Rubric], settings: Settings,
                      agent_user: str = "sentinel-bot") -> list[EvalResult]:
    results: list[EvalResult] = []
    for rubric in rubrics:
        role = settings.roles.get(rubric.role)
        if role is None:
            results.append(EvalResult(rubric.id, rubric.role, False,
                                      [f"role '{rubric.role}' is not in config/pipeline.yml"],
                                      rubric.scenario, rubric.invariant))
            continue
        prompt = build_system_prompt(settings, role, agent_user)
        results.append(evaluate_prompt(rubric, prompt))
    return results


def render(results: list[EvalResult]) -> str:
    passed = sum(1 for r in results if r.passed)
    lines = [f"PROMPT EVALS: {passed}/{len(results)} passed", ""]
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        lines.append(f"  [{mark}] {r.id}  ({r.role})")
        if not r.passed:
            lines.append(f"         invariant: {r.invariant}")
            for m in r.missing:
                lines.append(f"         missing concept: {m}")
    if passed != len(results):
        lines.append("")
        lines.append("A FAIL means a role's system prompt no longer encodes the invariant — "
                     "check that role's doc (or the shared docs / runtime preamble).")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Optional model-backed capture (opt-in; not run in CI)
# --------------------------------------------------------------------------- #

async def _capture(rubrics: list[Rubric], settings: Settings, model: str) -> int:
    from .llm import LLM
    llm = LLM(settings.litellm_base_url, settings.litellm_api_key, model)
    try:
        for rubric in rubrics:
            role = settings.roles.get(rubric.role)
            if role is None or not rubric.probe:
                continue
            system = build_system_prompt(settings, role, "sentinel-bot")
            print(f"\n===== {rubric.id} ({rubric.role}) =====")
            print(f"scenario: {rubric.scenario}")
            try:
                msg = await llm.chat([{"role": "system", "content": system},
                                      {"role": "user", "content": rubric.probe}],
                                     role=rubric.role)
                print("--- model response (for human review) ---")
                print((msg.content or "").strip() or "(no content)")
            except Exception as e:  # noqa: BLE001 — capture mode is best-effort
                print(f"(model call failed: {type(e).__name__}: {e})")
    finally:
        await llm.close()
    print("\nCapture mode is for human review only — it is not auto-graded.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sentinel.eval_prompts",
        description="Role prompt regression evals: assert each role's system prompt still "
                    "encodes its key behavioral invariants (deterministic, no LLM).")
    parser.add_argument("--rubrics", type=Path, default=Path(DEFAULT_RUBRIC_PATH),
                        help=f"rubric file (default: {DEFAULT_RUBRIC_PATH})")
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="output format (default: text)")
    parser.add_argument("--model", default=None,
                        help="OPT-IN: run each scenario against this LiteLLM model and print "
                             "the response for human review (needs LiteLLM env; not auto-graded)")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    # Placeholder (non-secret) env so the suite runs from a clean checkout; a real
    # --model run still needs real LiteLLM credentials in the environment.
    for name, value in (("JIRA_BASE_URL", "https://demo.invalid"), ("JIRA_PAT", "demo"),
                        ("JIRA_PROJECT_KEY", "SENT"),
                        ("LITELLM_BASE_URL", "https://demo.invalid"),
                        ("LITELLM_API_KEY", "demo")):
        os.environ.setdefault(name, value)

    settings = load_settings("config/pipeline.yml")
    rubrics = load_rubrics(args.rubrics)

    if args.model:
        return asyncio.run(_capture(rubrics, settings, args.model))

    results = run_deterministic(rubrics, settings)
    if args.format == "json":
        print(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        print(render(results))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
