#!/usr/bin/env python3
"""Drake (Tim Drake) - autonomous issue-creation agent.

Reads specs + roadmap + code-reality, identifies the next well-scoped gap, and
files GitHub issues that the implement agent (default: Lucius) can pick up. The
dedup gate, scope rules, and issue template all live in the prompt at
${ALFRED_HOME}/prompts/<codename>.md. `alfred-init` seeds a starter prompt
there; operators should edit it for their roadmap and stack. This runner is the
harness that loads the prompt, dispatches a Claude Code subprocess, enforces
fleet-wide spend / global-block / daily-cap, and reports.

Note on dedup: the prompt is responsible for prose-style dedup against open
issues, AND this runner pre-fetches the state-machine snapshot
(agent:in-flight / agent:pr-open / do-not-pickup / paused repos) so the
prompt sees them as "already handled" without having to re-query GitHub.
The same dedup primitives in lib/agent_runner.py (claim_issue, release_issue,
list_paused_repos) gate the implement-side flow; Drake only needs to read.

Failure modes (sentinel-driven, parsed from result.result_text):
  [DRAKE-OK]              -> success, issues created
  [DRAKE-NOOP]            -> nothing to file (everything deduped, queue saturated)
  [DRAKE-SCOPE-REJECTED]  -> candidate(s) failed the testable-acceptance-criteria gate;
                             spec section needs operator scoping before Drake can plan
  [DRAKE-DAILY-CAP-HIT]   -> rolling cap reached
  [DRAKE-OVER-BUDGET]     -> tool-call budget exhausted, partial results
  [DRAKE-ESCALATE]        -> gh auth dead / repo 404 / spec parse error
"""

from __future__ import annotations

import datetime as _dt
import os
import sys

sys.path.insert(
    0,
    (os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")) + "/lib",
)
from agent_runner import (
    ALFRED_HOME,
    GH_ORG,
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
    env_int,
    gh_json,
    invoke_agent_engine,
    is_globally_blocked,
    is_repo_paused,
    list_paused_repos,
    load_prompt,
    optional_env_int,
    preflight,
    run,
    set_global_block,
    short,
    slack_post,
    with_lock,
)

AGENT = os.environ.get("AGENT_CODENAME", "drake")
DRAKE_ENGINE = agent_engine(AGENT, default="hybrid")
LAUNCHD_LABEL = os.environ.get("LAUNCHD_LABEL", f"my.fleet.{AGENT}")

DRAKE_REPOS = [r.strip() for r in os.environ.get("ALFRED_DRAKE_REPOS", "").split(",") if r.strip()]


def _build_state_machine_context() -> str:
    """Snapshot the issues already in the lifecycle so Drake can skip them.

    Drake's prompt does prose-style dedup against open issues, but it has no
    visibility into the agent:in-flight / agent:pr-open / do-not-pickup
    state machine. Pre-fetch a structured snapshot and pass it in so Drake
    treats those issues as already-handled.
    """
    paused = list_paused_repos()
    in_flight: list[str] = []
    pr_open: list[str] = []
    do_not_pickup: list[str] = []
    for repo in DRAKE_REPOS:
        if is_repo_paused(repo):
            continue
        for label, sink in [
            ("agent:in-flight", in_flight),
            ("agent:pr-open", pr_open),
            ("do-not-pickup", do_not_pickup),
        ]:
            issues = gh_json(
                [
                    "gh",
                    "issue",
                    "list",
                    "-R",
                    f"{GH_ORG}/{repo}",
                    "--label",
                    label,
                    "--state",
                    "open",
                    "--json",
                    "number,title",
                    "--limit",
                    "30",
                ],
                default=[],
            )
            for i in issues:
                sink.append(f"{GH_ORG}/{repo}#{i['number']} - {i.get('title', '')[:80]}")
    parts = []
    if paused:
        parts.append("REPOS CURRENTLY PAUSED (do NOT file issues here):")
        for r in paused:
            parts.append(f"  - {r}")
    if in_flight:
        parts.append("ISSUES ALREADY IN-FLIGHT (an agent is working these; do NOT duplicate):")
        for entry in in_flight:
            parts.append(f"  - {entry}")
    if pr_open:
        parts.append("ISSUES WITH OPEN PR (do NOT duplicate; PR will close on merge):")
        for entry in pr_open:
            parts.append(f"  - {entry}")
    if do_not_pickup:
        parts.append("ISSUES MARKED do-not-pickup (operator owns these; do NOT duplicate):")
        for entry in do_not_pickup:
            parts.append(f"  - {entry}")
    if not parts:
        return ""
    return "\n## State-machine snapshot (live)\n\n" + "\n".join(parts) + "\n"


# Prompt path: alfred-init seeds this file, and the operator can customize it.
PROMPT_PATH = ALFRED_HOME / "prompts" / f"{AGENT}.md"
DAILY_ISSUE_CAP_DEFAULT = 200
DAILY_ISSUE_CAP = env_int(
    "ALFRED_DRAKE_DAILY_ISSUE_CAP",
    default=DAILY_ISSUE_CAP_DEFAULT,
    minimum=20,
)

PREFLIGHT = PreflightSpec(
    agent=AGENT,
    bins=[*engine_preflight_bins(DRAKE_ENGINE), "gh", "git"],
    require_gh_auth=True,
    # Drake reads across the entire workspace; missing checkouts are advisory only.
    require_workspace_repos=DRAKE_REPOS,
)


def _issues_authored_in_last_24h() -> int:
    """Count issues authored by the current gh user across GH_ORG in the
    last 24 hours. Used as a runner-level pre-flight cap; the prompt also
    re-checks this from inside the Claude session."""
    since = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    query_limit = min(max(DAILY_ISSUE_CAP + 50, 100), 1000)
    issues = gh_json(
        [
            "gh",
            "search",
            "issues",
            "--owner",
            GH_ORG,
            "--author",
            "@me",
            "--created",
            f">={since}",
            "--json",
            "url",
            "--limit",
            str(query_limit),
        ],
        default=[],
    )
    return len(issues) if isinstance(issues, list) else 0


def build_prompt() -> str:
    return (
        load_prompt(
            PROMPT_PATH,
            extra_vars={
                "AGENT_CODENAME": AGENT.title(),
                "FEATURE_DEV_CODENAME": os.environ.get(
                    "AGENT_CODENAME_FEATURE_DEV", "lucius"
                ).title(),
                "GH_ORG": GH_ORG,
                "ALFRED_HOME": str(ALFRED_HOME),
                "PLANNER_REPOS": ",".join(DRAKE_REPOS),
                "WORKSPACE_ROOT": str(WORKSPACE_ROOT),
            },
        )
        + _build_state_machine_context()
    )


def main() -> int:
    with_lock(AGENT)

    try:
        preflight(PREFLIGHT)
    except PreflightFailed:
        return 0

    if doctor_mode():
        print(f"[{AGENT.upper()}-DOCTOR-OK]")
        return 0

    if not DRAKE_REPOS:
        print(f"[{AGENT.upper()}-IDLE] no repos configured (set ALFRED_DRAKE_REPOS)")
        return 0

    events = EventLog(agent=AGENT)
    events.emit("firing_started")

    blocked = is_globally_blocked()
    if blocked:
        print(f"[{AGENT.upper()}-GLOBAL-BLOCKED] {blocked}. Skipping firing.")
        return 0

    spend = SpendState(AGENT)
    rate_blocked = spend.is_blocked()
    if rate_blocked:
        print(f"[{AGENT.upper()}-RATE-LIMITED] {rate_blocked}. Skipping firing.")
        return 0
    if spend.state.get("consecutive_failures", 0) >= 5:
        msg = (
            f"[{AGENT.upper()}-FAIL-STREAK] {spend.state['consecutive_failures']} consecutive "
            "failures, 0 successes. Pausing for human review."
        )
        print(msg)
        slack_post(msg, severity="alert")
        events.emit(
            "agent_paused",
            reason="fail_streak",
            consecutive_failures=spend.state["consecutive_failures"],
        )
        events.emit("firing_complete", outcome="paused_fail_streak")
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], timeout=10)
        return 0

    # Pre-flight daily cap check at the runner level. Skips the firing entirely
    # without burning an LLM turn if we're already at the wall.
    today_count = _issues_authored_in_last_24h()
    if today_count >= DAILY_ISSUE_CAP:
        msg = (
            f"[{AGENT.upper()}-DAILY-CAP-HIT] {today_count} issues created by @me in last 24h "
            f">= cap of {DAILY_ISSUE_CAP}. Skipping firing."
        )
        print(msg)
        slack_post(
            f"⏸️ {AGENT.title()}: daily {DAILY_ISSUE_CAP}-issue cap reached, skipping firing."
        )
        return 0

    if not PROMPT_PATH.exists():
        msg = f"[{AGENT.upper()}-CONFIG-MISSING] prompt at {PROMPT_PATH} not found"
        print(msg)
        slack_post(msg, severity="alert")
        spend.increment(failures_today=1, consecutive_failures=1)
        return 0

    prompt = build_prompt()

    # Drake works in the workspace root so it can read across all product repos
    # without juggling paths. It only writes via gh; no file edits.
    def _on_engine_fallback(fallback_result):
        events.emit(
            "llm_fallback",
            from_engine="claude",
            to_engine="codex",
            reason=short(fallback_result.error_message or fallback_result.result_text, 240),
        )

    result, engine_used = invoke_agent_engine(
        prompt,
        engine=DRAKE_ENGINE,
        claude_fn=claude_invoke_streaming,
        codex_fn=codex_invoke,
        workdir=WORKSPACE_ROOT,
        claude_allowed_tools="Read,Bash,Grep,Glob",
        agent=AGENT,
        firing_id=events.firing_id,
        claude_max_turns=optional_env_int("ALFRED_DRAKE_MAX_TURNS", minimum=40),
        timeout=1800,  # 30 min cap; Drake reads + greps + creates, no compile
        codex_timeout=1800,
        codex_sandbox=codex_sandbox_for_agent(AGENT, default="workspace-write"),
        codex_bypass_approvals_and_sandbox=True,
        codex_add_dirs=[WORKSPACE_ROOT],
        on_fallback=_on_engine_fallback,
        memory_repo=f"{GH_ORG}/workspace" if GH_ORG else "workspace",
    )

    spend.increment(
        firings_today=1,
        turns_today=result.num_turns,
        cost_usd_today=result.cost_usd,
    )

    text = result.result_text or ""
    events.emit(
        "llm_invoke_done",
        engine=engine_used,
        turns=result.num_turns,
        subtype=result.subtype,
        success=result.success,
    )

    # Rate-limit / budget hits propagate to the fleet-wide global block so other
    # agents don't burn turns into the same wall.
    if result.subtype in ("error_budget", "error_rate_limit"):
        until = None
        if engine_used == "claude":
            until = set_global_block(hours=1, reason=f"{AGENT}-{result.subtype}")
        spend.increment(failures_today=1, consecutive_failures=1)
        if until:
            msg = (
                f"{AGENT.title()} hit Claude provider rate limit ({result.subtype}). Global block until "
                f"{until} - Claude agents will skip until then."
            )
        else:
            msg = (
                f"{AGENT.title()} hit provider rate limit ({result.subtype}, engine={engine_used}); "
                "Claude agents are not globally blocked."
            )
        print(msg)
        slack_post(msg, severity="alert")
        return 0

    if result.subtype == "error_max_turns":
        spend.increment(failures_today=1, consecutive_failures=1)
        msg = (
            f"⏸️ {AGENT.title()} hit max-turns cap ({result.num_turns}). "
            f"Last output: {short(text, 300)}"
        )
        print(msg)
        slack_post(msg, severity="warn")
        return 0

    if result.subtype != "success":
        spend.increment(failures_today=1, consecutive_failures=1)
        msg = (
            f"❌ {AGENT.title()} firing failed: engine={engine_used} "
            f"subtype={result.subtype} turns={result.num_turns}. {short(text, 300)}"
        )
        print(msg)
        slack_post(msg, severity="warn")
        return 0

    # Successful subprocess return. Parse the sentinel the prompt was instructed
    # to emit so we can report meaningfully.
    if "[DRAKE-DAILY-CAP-HIT]" in text:
        print(text[-400:])
        slack_post(f"⏸️ {AGENT.title()}: daily cap (in-prompt check). {short(text[-300:], 300)}")
        spend.increment(successes_today=1)
        return 0

    if "[DRAKE-OVER-BUDGET]" in text:
        spend.increment(failures_today=1, consecutive_failures=1)
        msg = f"⚠️ {AGENT.title()} hit prompt tool-call budget. {short(text[-400:], 400)}"
        print(msg)
        slack_post(msg, severity="warn")
        return 0

    if "[DRAKE-ESCALATE]" in text:
        spend.increment(failures_today=1, consecutive_failures=1)
        msg = f"{AGENT.title()} escalating: {short(text[-500:], 500)}"
        print(msg)
        slack_post(msg, severity="alert")
        return 0

    if "[DRAKE-SCOPE-REJECTED]" in text:
        # Drake encountered a spec section too vague to plan against.
        # Treat as a soft signal, not a failure, since Drake correctly
        # refused to file a low-quality issue. Surface to Slack so the
        # operator sees which spec area needs sharpening.
        spend.set(consecutive_failures=0)
        spend.increment(successes_today=1)
        msg = (
            f"⚠️ {AGENT.title()} refused to file vague candidate(s); "
            f"spec needs operator scoping. {short(text[-500:], 500)}"
        )
        print(msg)
        slack_post(msg)
        return 0

    if "[DRAKE-NOOP]" in text:
        # Healthy non-event: nothing to file this firing.
        events.emit("firing_complete", outcome="noop")
        print("[SILENT]")
        spend.set(consecutive_failures=0)
        spend.increment(successes_today=1)
        return 0

    if "[DRAKE-OK]" in text:
        spend.set(consecutive_failures=0)
        spend.increment(successes_today=1)
        # Surface the full closing report (issue URLs + counts) to Slack so the
        # operator can scan and click through.
        report = text[text.find("[DRAKE-OK]") :]
        events.emit(
            "firing_complete", outcome="ok", turns=result.num_turns, cost_usd=result.cost_usd
        )
        msg = f"📋 {AGENT.title()} firing complete (engine={engine_used}, turns={result.num_turns}, cost=${result.cost_usd:.2f})\n{short(report, 1500)}"
        print(msg)
        slack_post(msg)
        return 0

    # Success subtype but no recognised sentinel. Treat as soft failure: the
    # prompt didn't follow its closing-line contract.
    spend.increment(failures_today=1, consecutive_failures=1)
    msg = (
        f"⚠️ {AGENT.title()} returned success but emitted no [DRAKE-*] sentinel. "
        f"engine={engine_used} turns={result.num_turns}. Tail: {short(text[-400:], 400)}"
    )
    print(msg)
    slack_post(msg, severity="warn")
    return 0


if __name__ == "__main__":
    sys.exit(main())
