#!/usr/bin/env python3
"""``damian``, spec-level multi-repo bundle planner.

Damian sits between drake (single-repo issue filer) and batman
(cross-repo plan coordinator). Drake keeps the per-repo
``agent:implement`` queue full; damian keeps the multi-repo
``agent:bundle:<slug>`` queue full so batman has work to plan.

Wiring:

  - Reads ``GH_ORG`` for repo-qualified ``gh`` calls.
  - Reads ``DAMIAN_SCAN_REPOS`` (comma-separated repo slugs) to scope
    bundle filing. Empty means damian exits as a no-op — a fresh install
    is not assumed to know which repos are bundle-eligible.
  - Reads ``DAMIAN_SPEC_DIR`` (absolute path or path relative to
    ``WORKSPACE_ROOT``) for the spec markdown the default parser walks.
  - Loads the operator-customizable prompt at
    ``${ALFRED_HOME}/prompts/<codename>.md`` (seeded by ``alfred-init``
    from ``prompts/spec-bundle-planner.md``).
  - Honours the fleet enable file: ``damian`` ships opt-in (like
    ``batman``) so the runner exits early until the operator enables it.

This file is the runner skeleton: preflight, build the candidate plan
via ``lib/damian_planner.py``, dispatch the LLM to actually file the
issues (when an engine is wired), and report. The candidate-list logic
lives in the library so a fleet can dry-run it with no LLM and no gh.

Failure modes (sentinel-driven, parsed from ``result.result_text`` when
the LLM is wired):

  ``[DAMIAN-OK]``               -> success, bundles created
  ``[DAMIAN-NOOP]``             -> nothing to file (deduped or shipped)
  ``[DAMIAN-DAILY-CAP-HIT]``    -> per-firing bundle cap reached
  ``[DAMIAN-OVER-BUDGET]``      -> LLM tool-call budget exhausted
  ``[DAMIAN-ESCALATE]``         -> gh auth dead / repo 404 / parse error
  ``[DAMIAN-BUNDLE-ROLLED-BACK]`` -> partial bundle rolled back
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for candidate in (
    _HERE.parent / "lib",
    Path(os.environ.get("ALFRED_HOME", "")) / "lib",
):
    if candidate.exists():
        candidate_path = str(candidate)
        if candidate_path in sys.path:
            sys.path.remove(candidate_path)
        sys.path.insert(0, candidate_path)

from agent_runner import (  # noqa: E402
    ALFRED_HOME,
    GH_ORG,
    WORKSPACE_ROOT,
    PreflightSpec,
    agent_engine,
    doctor_mode,
    gh_json,
    is_agent_enabled,
    load_prompt,
    preflight,
    slack_post,
    with_lock,
)
from damian_planner import (  # noqa: E402
    PlannerConfig,
    SpecBundlePlanner,
    render_plan_for_prompt,
)

CODENAME = os.environ.get("AGENT_CODENAME", "damian")
DAMIAN_ENGINE = agent_engine(CODENAME, default="hybrid")

# Prompt path: alfred-init seeds the role file at this location and the
# operator can rename / customise it. Default seed lives at
# ``prompts/spec-bundle-planner.md`` in this repo.
PROMPT_PATH = ALFRED_HOME / "prompts" / f"{CODENAME}.md"


def _resolve_spec_dir(config: PlannerConfig) -> Path | None:
    """Resolve ``DAMIAN_SPEC_DIR`` against ``WORKSPACE_ROOT`` when relative.

    Returns ``None`` when nothing is configured so the planner emits the
    expected empty plan instead of guessing at a path on disk.
    """
    raw = config.spec_dir
    if raw is None:
        return None
    if raw.is_absolute():
        return raw
    return (WORKSPACE_ROOT / raw).resolve()


def _build_planner(config: PlannerConfig) -> SpecBundlePlanner:
    """Compose a planner with the production gh client."""

    def _gh(cmd: list[str]) -> list[dict]:
        rows = gh_json(cmd, default=[])
        return rows if isinstance(rows, list) else []

    return SpecBundlePlanner.from_config(
        config,
        gh_client=_gh,
        gh_org=GH_ORG,
    )


def main() -> int:
    if doctor_mode():
        print(f"[{CODENAME.upper()}-DOCTOR-OK]")
        return 0

    # Damian is opt-in: it files multi-repo bundles, which only makes
    # sense once the operator has at least two repos wired and a spec
    # directory the planner can read. Fresh installs stay quiet until
    # ``alfred enable damian`` flips the gate.
    if not is_agent_enabled(CODENAME, default=False):
        print(
            f"[DAMIAN-SKIP] {CODENAME} not enabled in fleet file; "
            f"run `alfred enable {CODENAME}` to opt in.",
            file=sys.stderr,
        )
        return 0

    spec = PreflightSpec(
        agent=CODENAME,
        env_vars=["ALFRED_HOME", "WORKSPACE_ROOT", "GH_ORG"],
        bins=["gh", "git"],
        require_gh_auth=True,
    )
    try:
        preflight(spec)
    except Exception as e:
        print(f"[DAMIAN-PREFLIGHT-FAIL] {e}", file=sys.stderr)
        return 0

    with_lock(CODENAME)

    config = PlannerConfig.from_env()
    if not config.scan_repos:
        print(
            "[DAMIAN-IDLE] no repos configured (set DAMIAN_SCAN_REPOS=your-org/your-backend,your-org/your-frontend)",
        )
        return 0

    spec_dir = _resolve_spec_dir(config)
    if spec_dir is None:
        print(
            "[DAMIAN-IDLE] no spec directory configured "
            "(set DAMIAN_SPEC_DIR to a markdown spec dir relative to WORKSPACE_ROOT)",
        )
        return 0

    planner = _build_planner(config)
    plan = planner.build_plan(spec_dir)

    summary = (
        f"candidates={len(plan.bundles)} "
        f"single_repo_specs={plan.rejected_single_repo} "
        f"unparseable={plan.skipped_unparseable}"
    )

    if plan.is_empty:
        print(f"[DAMIAN-NOOP] {summary}")
        return 0

    if not PROMPT_PATH.exists():
        # No prompt seeded yet: surface the candidate plan as a Slack
        # signal so the operator can run alfred-init / seed the prompt
        # before damian starts filing bundles unattended.
        body = render_plan_for_prompt(plan)
        msg = (
            f"[DAMIAN-PLAN-DRAFTED] prompt at {PROMPT_PATH} not found; "
            f"draft plan ready ({summary}). Seed the prompt to enable filing.\n{body}"
        )
        print(msg)
        slack_post(msg, severity="info")
        return 0

    # Render the candidate plan into the prompt context so the LLM can
    # focus on filing instead of re-walking the spec directory. The
    # actual ``gh issue create`` chain (label set, all-or-nothing
    # filing, slug uniqueness sweep) is the prompt's responsibility; the
    # runner stops here in the OSS build. Site-specific fleets layer a
    # ``claude_invoke`` / ``codex_invoke`` call on top using the
    # ``invoke_agent_engine`` helper from ``lib/agent_runner.py``.
    prompt_context = render_plan_for_prompt(plan)
    base_prompt = load_prompt(
        PROMPT_PATH,
        extra_vars={
            "AGENT_CODENAME": CODENAME.title(),
            "GH_ORG": GH_ORG,
            "ALFRED_HOME": str(ALFRED_HOME),
            "WORKSPACE_ROOT": str(WORKSPACE_ROOT),
            "PLANNER_REPOS": ",".join(config.scan_repos),
            "DAILY_BUNDLE_CAP": str(config.daily_bundle_cap),
            "BUNDLES_TODAY": "0",
        },
    )
    composed = base_prompt + "\n\n" + prompt_context

    # The OSS package ships the planner as plan-only and prints the
    # composed prompt so operators can wire it to ``claude -p`` (or a
    # codex equivalent) without prescribing one engine. The deterministic
    # part — what to file, which repos, which slugs — is what matters
    # for the OSS surface; the LLM call itself is fleet-specific.
    print(f"[DAMIAN-PLAN-DRAFTED] {summary} engine={DAMIAN_ENGINE}")
    print(composed)
    slack_post(
        f"[DAMIAN-PLAN-DRAFTED] {summary} engine={DAMIAN_ENGINE}",
        severity="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
