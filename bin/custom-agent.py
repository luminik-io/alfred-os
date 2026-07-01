#!/usr/bin/env python3
"""Generic runner for operator-defined Alfred agents.

Custom agents are created from the local manifest at
``$ALFRED_HOME/state/custom-agents/custom-agents.json`` and scheduled through
the same launchd/systemd path as built-in agents. This runner is intentionally
boring: it wraps the operator-authored prompt in normal Alfred lifecycle
plumbing so a custom role gets locks, preflight, event logs, spend ledgers,
runtime memory, engine routing, and Slack reporting.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_LIB = _HERE.parent / "lib"
sys.path.insert(0, (os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")) + "/lib")
if str(_REPO_LIB) not in sys.path:
    sys.path.insert(0, str(_REPO_LIB))

from agent_runner import (  # noqa: E402
    ALFRED_HOME,
    GH_ORG,
    WORKSPACE,
    WORKSPACE_ROOT,
    EventLog,
    PreflightFailed,
    PreflightSpec,
    SpendState,
    agent_engine,
    claude_invoke_streaming,
    codex_invoke,
    codex_sandbox_for_agent,
    doctor_mode,
    engine_preflight_bins,
    invoke_agent_engine,
    is_globally_blocked,
    local_repo_dir,
    maybe_set_global_block_for_result,
    optional_env_int,
    preflight,
    reported_subtype,
    set_dry_run,
    short,
    slack_post,
    with_lock,
)
from custom_agents import CustomAgentStore  # noqa: E402

if "--dry-run" in sys.argv:
    set_dry_run(True)

AGENT = os.environ.get("AGENT_CODENAME", "").strip().lower()


def _store() -> CustomAgentStore:
    return CustomAgentStore.from_env()


def _repo_context(repos: tuple[str, ...]) -> tuple[Path, str]:
    """Return a safe workdir plus a prompt-readable repo scope block."""
    if not repos:
        return _safe_workdir(), "No repository scope configured. Work from the workspace root."
    lines: list[str] = []
    workdir: Path | None = None
    for repo in repos:
        local = local_repo_dir(repo)
        path = WORKSPACE / local
        marker = "present" if (path / ".git").exists() else "missing"
        lines.append(f"- {repo}: {path} ({marker})")
        if workdir is None and (path / ".git").exists():
            workdir = path
    return workdir or _safe_workdir(), "Repository scope:\n" + "\n".join(lines)


def _safe_workdir() -> Path:
    for candidate in (WORKSPACE, WORKSPACE_ROOT, ALFRED_HOME, Path.home()):
        if candidate.exists():
            return candidate
    try:
        WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
        return WORKSPACE_ROOT
    except OSError:
        return Path.home()


def _compose_prompt(agent) -> str:
    _workdir, repo_block = _repo_context(agent.repos)
    return f"""You are {agent.display_name}, an Alfred custom runtime agent.

Runtime codename: {agent.codename}
Role: {agent.role_title}
Purpose: {agent.purpose}
Engine mode: {agent.engine}

{repo_block}

Operator instructions:
{agent.prompt}

Rules:
- Be concrete and concise.
- Use the repository scope above as the source of truth when one is configured.
- Treat this generic custom runner as read-only unless the operator explicitly configured a writable engine sandbox.
- Do not fabricate numbers, URLs, or file paths.
- If work cannot proceed, explain the blocking condition and the exact next action.
- End with a short outcome line that Alfred can post to Slack.
"""


def main() -> int:
    if not AGENT:
        print("[CUSTOM-AGENT-ERROR] AGENT_CODENAME is not set", file=sys.stderr)
        return 2
    agent = _store().get(AGENT)
    if agent is None:
        print(f"[{AGENT.upper()}-CUSTOM-MISSING] no custom-agent manifest row")
        return 0
    if not agent.enabled:
        print(f"[{AGENT.upper()}-CUSTOM-DISABLED] disabled in custom-agent manifest")
        return 0

    with_lock(AGENT)

    engine = agent_engine(AGENT, default=agent.engine)
    preflight_spec = PreflightSpec(
        agent=AGENT,
        bins=[*engine_preflight_bins(engine), "git"],
        require_gh_auth=False,
    )
    try:
        preflight(preflight_spec)
    except PreflightFailed:
        return 0

    if doctor_mode():
        print(f"[{AGENT.upper()}-DOCTOR-OK]")
        return 0

    events = EventLog(agent=AGENT)
    events.emit("firing_started")

    blocked = is_globally_blocked()
    if blocked:
        print(f"[{AGENT.upper()}-GLOBAL-BLOCKED] {blocked}. Skipping firing.")
        events.emit("firing_complete", outcome="global-blocked")
        return 0

    spend = SpendState(AGENT)
    blocked = spend.is_blocked()
    if blocked:
        print(f"[{AGENT.upper()}-RATE-LIMITED] {blocked}. Skipping firing.")
        events.emit("firing_complete", outcome="agent-blocked")
        return 0

    workdir, _repo_block = _repo_context(agent.repos)
    prompt = _compose_prompt(agent)

    def _on_engine_fallback(fallback_result):
        events.emit(
            "llm_fallback",
            from_engine="claude",
            to_engine="codex",
            reason=fallback_result.error_message or fallback_result.result_text,
        )

    result, engine_used = invoke_agent_engine(
        prompt,
        engine=engine,
        claude_fn=claude_invoke_streaming,
        codex_fn=codex_invoke,
        workdir=workdir,
        claude_allowed_tools="Read,Grep,Glob",
        agent=AGENT,
        firing_id=events.firing_id,
        claude_max_turns=optional_env_int(
            f"ALFRED_{AGENT.upper().replace('-', '_')}_MAX_TURNS", minimum=20
        ),
        timeout=900,
        codex_timeout=900,
        codex_sandbox=codex_sandbox_for_agent(AGENT, default="read-only"),
        codex_bypass_approvals_and_sandbox=False,
        on_fallback=_on_engine_fallback,
        memory_repo=agent.repos[0] if agent.repos else (f"{GH_ORG}/workspace" if GH_ORG else None),
        memory_query=f"{agent.role_title}: {agent.purpose}",
    )
    spend.increment(firings_today=1, turns_today=result.num_turns, cost_usd_today=result.cost_usd)
    root_subtype = reported_subtype(result)
    events.emit(
        "llm_invoke_done",
        engine=engine_used,
        turns=result.num_turns,
        subtype=root_subtype,
        raw_subtype=result.subtype,
        success=result.success,
    )

    if not result.success:
        until = maybe_set_global_block_for_result(AGENT, result, engine_used=engine_used)
        if until:
            msg = (
                f"{agent.display_name} hit provider rate limit "
                f"({result.subtype}, engine={engine_used}). Global block until {until}."
            )
            print(msg)
            slack_post(msg, severity="alert")
            events.emit("firing_complete", outcome=f"llm-{result.subtype}", engine=engine_used)
            return 0
        spend.increment(failures_today=1, consecutive_failures=1)
        detail = short(result.error_message or result.result_text or root_subtype, 240)
        msg = f"[{AGENT.upper()}-FAILED] {agent.display_name}: {detail}"
        print(msg)
        slack_post(msg, severity="warn")
        events.emit("firing_complete", outcome=f"llm-{root_subtype}", engine=engine_used)
        return 0

    spend.increment(successes_today=1)
    spend.set(consecutive_failures=0)
    text = (result.result_text or "").strip()
    summary = short(text.splitlines()[-1] if text.splitlines() else text, 300)
    msg = (
        f"{agent.display_name} complete (engine={engine_used}, turns={result.num_turns}): {summary}"
    )
    print(msg)
    slack_post(msg, severity="info")
    events.emit("firing_complete", outcome="complete", engine=engine_used)
    return 0


if __name__ == "__main__":
    sys.exit(main())
