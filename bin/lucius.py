#!/usr/bin/env python3
"""Lucius - feature dev agent. Picks an `agent:implement` issue, delegates to claude -p."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")) + "/lib")
from agent_runner import (
    GH_ORG,
    WORKSPACE,
    EventLog,
    PreflightFailed,
    PreflightSpec,
    SpendState,
    agent_engine,
    claim_issue,
    claude_invoke_streaming,
    codex_invoke,
    codex_sandbox_for_agent,
    commit_trailer,
    doctor_mode,
    engine_preflight_bins,
    gh_issue_comment,
    gh_issue_edit,
    gh_json,
    gh_pr_create,
    invoke_agent_engine,
    is_globally_blocked,
    is_repo_paused,
    make_worktree,
    optional_env_int,
    preflight,
    release_issue,
    remove_worktree,
    run,
    set_global_block,
    short,
    slack_post,
    with_lock,
)

# Codename is operator-overridable. The bin file name keeps the Batman default;
# the launchd plist EnvironmentVariables can set AGENT_CODENAME to rename the
# agent at runtime without touching the source. Slack messages use AGENT.title()
# so a renamed agent renders cleanly.
AGENT = os.environ.get("AGENT_CODENAME", "lucius")
LUCIUS_ENGINE = agent_engine(AGENT, default="hybrid")

# Launchd plist label used for the auto-pause path. Defaults to a generic name;
# override in the plist EnvironmentVariables to match your label scheme.
LAUNCHD_LABEL = os.environ.get("LAUNCHD_LABEL", f"my.fleet.{AGENT}")

# Repos this agent watches. Comma-separated env var lets the operator scope the
# fleet without editing source. Empty list = idle exit.
LUCIUS_REPOS = [
    r.strip() for r in os.environ.get("ALFRED_LUCIUS_REPOS", "").split(",") if r.strip()
]

PREFLIGHT = PreflightSpec(
    agent=AGENT,
    bins=[*engine_preflight_bins(LUCIUS_ENGINE), "gh", "git"],
    require_gh_auth=True,
    # Repo dirs are resolved by name under WORKSPACE; absent dirs fail preflight.
    require_workspace_repos=LUCIUS_REPOS,
)

# Daily turn cap before auto-pausing the launchd agent. Override via env var.
DAILY_TURN_CAP = int(os.environ.get("ALFRED_LUCIUS_TURN_CAP", "5000"))


def _load_pre_push_config(agent_codename: str) -> dict[str, str]:
    """Load per-repo pre-push commands from ${HOME}/.alfredrc.d/<codename>.yaml.

    TOML format:
        pre_push:
          backend: ./gradlew check
          frontend: npm run lint && npx tsc --noEmit

    Falls back to language defaults inferred from the repo name suffix:
      *-backend / *-api      -> ./gradlew check
      *-frontend / *-mobile / *-web -> npm run lint && npx tsc --noEmit
      *-nango                -> npm run lint && npx tsc --noEmit
      python repo (pyproject.toml) -> uv run ruff check . && uv run mypy . && uv run pytest
      else                   -> "" (no pre-push, agent reports it in PR body)
    """
    cfg_path = Path(os.path.expanduser(f"~/.alfredrc.d/{agent_codename}.yaml"))
    user_cfg: dict[str, str] = {}
    if cfg_path.exists():
        try:
            data = tomllib.loads(cfg_path.read_text())
            user_cfg = dict(data.get("pre_push", {}) or {})
        except (OSError, tomllib.TOMLDecodeError):
            user_cfg = {}

    out: dict[str, str] = {}
    for repo in LUCIUS_REPOS:
        if repo in user_cfg:
            out[repo] = user_cfg[repo]
            continue
        # Default by suffix
        if repo.endswith("-backend") or repo.endswith("-api"):
            out[repo] = "./gradlew check"
        elif repo.endswith(("-frontend", "-mobile", "-web", "-nango")):
            out[repo] = "npm run lint && npx tsc --noEmit"
        else:
            local_dir = WORKSPACE / repo
            if (local_dir / "pyproject.toml").exists():
                out[repo] = "uv run ruff check . && uv run mypy . && uv run pytest"
            else:
                out[repo] = ""
    return out


PRE_PUSH = _load_pre_push_config(AGENT)


def pick_issue() -> tuple[str, dict] | tuple[None, None]:
    """Find oldest open agent:implement issue across repos. Skip 3+ attempts.
    Skip paused repos."""
    for repo in LUCIUS_REPOS:
        if is_repo_paused(repo):
            continue
        issues = gh_json(
            [
                "gh",
                "issue",
                "list",
                "-R",
                f"{GH_ORG}/{repo}",
                "--label",
                "agent:implement",
                "--state",
                "open",
                "--json",
                "number,title,url,labels,createdAt,body",
                "--limit",
                "20",
            ],
            default=[],
        )
        if not issues:
            continue
        issues.sort(key=lambda i: i["createdAt"])
        for issue in issues:
            label_names = {lbl["name"] for lbl in issue.get("labels", [])}
            # Defensive: skip anything carrying a state-machine blocker. The
            # gh query already filters by agent:implement, but a fresh issue
            # could acquire one of these between query and pick.
            if label_names & {
                "agent:in-flight",
                "agent:pr-open",
                f"{AGENT}-pr-open",
                "do-not-pickup",
                "needs:human-scope",
            }:
                continue
            attempts = sum(1 for lbl in label_names if lbl.startswith(f"{AGENT}-attempt-"))
            if attempts >= 3:
                # Auto-mark needs:human-scope
                gh_issue_edit(
                    repo,
                    issue["number"],
                    add_labels=["needs:human-scope"],
                    remove_labels=["agent:implement"],
                )
                gh_issue_comment(
                    repo,
                    issue["number"],
                    f"{AGENT.title()}: 3 prior attempts failed to ship. Marking needs:human-scope.",
                )
                continue
            issue["_attempts"] = attempts
            return repo, issue
    return None, None


def build_prompt(repo: str, issue: dict, wt: Path, branch: str, firing_id: str) -> str:
    repo_claude_md = ""
    md = WORKSPACE / repo / "CLAUDE.md"
    if md.exists():
        repo_claude_md = md.read_text()

    trailer = commit_trailer(
        AGENT,
        firing_id,
        extra={"issue": f"{GH_ORG}/{repo}#{issue['number']}"},
    )

    return f"""You are {AGENT.title()}, implementing GitHub issue #{issue["number"]} in {GH_ORG}/{repo}.

Issue title: {issue["title"]}
Issue URL: {issue["url"]}

Issue body:
{issue["body"]}

You are working in this worktree: {wt}
Branch: {branch}

The repo CLAUDE.md (pre-cached so you do not have to read it):
---
{repo_claude_md}
---

Constraints:
- Surgical edits only. Read git log + existing files before writing.
- Follow patterns already in the repo. Look at neighboring files when in doubt.
- No em-dashes anywhere. No "unlock", "leverage", "seamless", "transform". No fabricated numbers.
- Never push, never open a PR, never merge. Just edit + commit locally on this branch.
- If you discover the work is already implemented, do NOT commit. Print "[ALREADY-IMPLEMENTED] file:line" and exit.

Pre-push checks (must pass before you commit):
{PRE_PUSH.get(repo, "(none configured for this repo)")}

When done implementing:
1. Stage the files you changed.
2. Commit with conventional-commit message: <type>(<scope>): <subject>. Body explains WHY not WHAT. Single-line subject under 72 chars.
3. The commit message body MUST end with this exact trailer block (blank line before it, no quoting, no rewording):

{trailer}

4. Print: "[OK] commit <sha> | files=<N> | <one-line-summary>"

The trailer is a forensic anchor. `git log --grep "Agent-Firing-Id: {firing_id}"` should find this commit and only this commit. Do not modify the codename, firing-id, or issue lines.

If you cannot complete in your turn budget:
- Commit any partial work that compiles cleanly. Include the trailer block above on the partial commit too.
- Print: "[PARTIAL] <progress and what remains>"

If you hit an error you cannot resolve:
- Print: "[BLOCKED] <reason>"
"""


def release_wip_salvage(repo: str, issue_num: int, firing_id: str, pr_url: str | None) -> None:
    if pr_url:
        release_issue(
            repo,
            issue_num,
            codename=AGENT,
            firing_id=firing_id,
            outcome="partial",
            transition_to="agent:pr-open",
            pr_url=pr_url,
        )
        return

    release_issue(
        repo,
        issue_num,
        codename=AGENT,
        firing_id=firing_id,
        outcome="partial-pr-create-failed",
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

    if not LUCIUS_REPOS:
        print(f"[{AGENT.upper()}-IDLE] no repos configured (set ALFRED_LUCIUS_REPOS)")
        return 0

    # Per-firing event log — every meaningful step gets a record so a Slack
    # post-mortem on a confused firing reads as `tail events.jsonl | jq`.
    events = EventLog(agent=AGENT)
    events.emit("firing_started")

    blocked = is_globally_blocked()
    if blocked:
        print(f"[{AGENT.upper()}-GLOBAL-BLOCKED] {blocked}. Skipping firing.")
        return 0
    spend = SpendState(AGENT)

    # Daily caps
    blocked = spend.is_blocked()
    if blocked:
        print(f"[{AGENT.upper()}-RATE-LIMITED] {blocked}. Skipping firing.")
        return 0
    if spend.state["turns_today"] >= DAILY_TURN_CAP:
        msg = f"[{AGENT.upper()}-DAILY-CAP] turns_today={spend.state['turns_today']} >= {DAILY_TURN_CAP}."
        print(msg)
        slack_post(msg + f" Auto-pausing {LAUNCHD_LABEL}.", severity="alert")
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], timeout=10)
        return 0
    if spend.state["consecutive_failures"] >= 8:
        msg = f"[{AGENT.upper()}-FAIL-STREAK] {spend.state['consecutive_failures']} consecutive failures, 0 successes. Pausing for human review."
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

    repo, issue = pick_issue()
    if not repo:
        events.emit("firing_complete", outcome="silent_no_work")
        print("[SILENT]")
        return 0

    issue_num = issue["number"]

    # Pre-flight scoping
    body_len = len(issue.get("body") or "")
    if body_len > 8000:
        gh_issue_comment(
            repo,
            issue_num,
            f"{AGENT.title()}: issue body is {body_len} chars - too cross-cutting. Marking needs:human-scope.",
        )
        gh_issue_edit(
            repo, issue_num, add_labels=["needs:human-scope"], remove_labels=["agent:implement"]
        )
        print(f"[{AGENT.upper()}-SKIPPED] #{issue_num} body too large ({body_len} chars)")
        return 0

    next_attempt = issue["_attempts"] + 1
    gh_issue_edit(repo, issue_num, add_labels=[f"{AGENT}-attempt-{next_attempt}"])

    # Atomic-ish claim. Refused if any other agent has agent:in-flight,
    # if a PR is already open, or if the operator set do-not-pickup. Race
    # detection inside claim_issue backs out cleanly if we lost.
    if not claim_issue(repo, issue_num, codename=AGENT, firing_id=events.firing_id):
        events.emit(
            "firing_complete", outcome="dedup_skip", repo=f"{GH_ORG}/{repo}", number=issue_num
        )
        msg = f"[{AGENT.upper()}-DEDUP-SKIP] #{issue_num} already claimed / has PR / paused"
        print(msg)
        return 0

    # Worktree
    try:
        wt, branch = make_worktree(repo, AGENT, str(issue_num))
    except RuntimeError as e:
        msg = f"[{AGENT.upper()}-ERROR] {e}"
        print(msg)
        # Release the claim we just took so the next firing can retry.
        release_issue(
            repo, issue_num, codename=AGENT, firing_id=events.firing_id, outcome="worktree-failed"
        )
        spend.increment(failures_today=1, consecutive_failures=1)
        return 0

    # Invoke the configured LLM engine.
    events.emit("issue_picked", repo=f"{GH_ORG}/{repo}", number=issue_num, attempt=next_attempt)
    events.emit("worktree_created", branch=branch, path=str(wt))
    prompt = build_prompt(repo, issue, wt, branch, firing_id=events.firing_id)
    # Persist prompt + raw result for debugging
    debug_dir = Path(f"/tmp/{AGENT}-debug-{issue_num}-{int(__import__('time').time())}")
    debug_dir.mkdir(exist_ok=True)
    (debug_dir / "prompt.txt").write_text(prompt)

    # Per-firing turn cap intentionally unset by default. The previous
    # hard ceiling on ``max_turns`` could produce no-output runs on
    # cross-file work where Lucius needs to read context, edit, and run
    # pre-push checks. The wall-clock ``timeout`` below is the only real
    # ceiling now; ``claude_invoke_streaming`` translates a ``None`` cap to
    # ``--max-turns _CLAUDE_UNLIMITED_TURNS`` so the CLI's hidden 40-
    # turn default cannot kick in. ``ALFRED_LUCIUS_MAX_TURNS`` exists
    # as an emergency / debug knob; ``optional_env_int`` clamps it to
    # a sensible floor.
    def _on_engine_fallback(fallback_result):
        events.emit(
            "llm_fallback",
            from_engine="claude",
            to_engine="codex",
            reason=short(fallback_result.error_message or fallback_result.result_text, 240),
        )

    result, engine_used = invoke_agent_engine(
        prompt,
        engine=LUCIUS_ENGINE,
        claude_fn=claude_invoke_streaming,
        codex_fn=codex_invoke,
        workdir=wt,
        claude_allowed_tools="Read,Edit,Write,Bash,Grep",
        agent=AGENT,
        firing_id=events.firing_id,
        claude_max_turns=optional_env_int("ALFRED_LUCIUS_MAX_TURNS", minimum=40),
        timeout=2400,  # 40 min cap; compile + claude can stretch
        codex_timeout=2400,
        codex_sandbox=codex_sandbox_for_agent(AGENT, default="workspace-write"),
        codex_bypass_approvals_and_sandbox=True,
        on_fallback=_on_engine_fallback,
    )
    import json as _json

    (debug_dir / "result.json").write_text(_json.dumps(result.raw, indent=2)[:200000])
    (debug_dir / "result-text.txt").write_text(result.result_text or "")

    spend.increment(firings_today=1, turns_today=result.num_turns, cost_usd_today=result.cost_usd)
    events.emit(
        "llm_invoke_done",
        engine=engine_used,
        turns=result.num_turns,
        subtype=result.subtype,
        success=result.success,
    )

    # Branch on result
    if result.subtype == "success":
        # Did the engine commit?
        new_commits = run(
            ["git", "rev-list", "origin/main..HEAD"], cwd=str(wt), timeout=10
        ).stdout.strip()
        commit_count = len([lbl for lbl in new_commits.splitlines() if lbl.strip()])

        if "[ALREADY-IMPLEMENTED]" in result.result_text:
            gh_issue_comment(
                repo,
                issue_num,
                f"{AGENT.title()} full-context check: {short(result.result_text, 300)}\n\nClosing as duplicate.",
            )
            gh_issue_edit(repo, issue_num, add_labels=["done-already"])
            release_issue(
                repo,
                issue_num,
                codename=AGENT,
                firing_id=events.firing_id,
                outcome="already-implemented",
                transition_to="agent:done",
            )
            run(["gh", "issue", "close", str(issue_num), "-R", f"{GH_ORG}/{repo}"], timeout=20)
            remove_worktree(repo, wt)
            spend.set(consecutive_failures=0)
            spend.increment(successes_today=1)
            msg = f"✅ {AGENT.title()} #{issue_num} already implemented - closed without PR. turns={result.num_turns}"
            print(msg)
            slack_post(msg)
            return 0

        if commit_count == 0:
            # Salvage: check for unstaged changes and push as draft WIP PR
            status = run(["git", "status", "--porcelain"], cwd=str(wt), timeout=10).stdout.strip()
            if status:
                operator_email = os.environ.get("OPERATOR_EMAIL", f"{AGENT}@example.com")
                # There ARE uncommitted changes — save them as a draft PR
                run(["git", "add", "-A"], cwd=str(wt), timeout=30)
                stat = run(
                    ["git", "diff", "--cached", "--stat"], cwd=str(wt), timeout=10
                ).stdout.strip()
                run(
                    [
                        "git",
                        "-c",
                        f"user.email={operator_email}",
                        "-c",
                        f"user.name={AGENT.title()}",
                        "commit",
                        "-m",
                        f"WIP: partial implementation of #{issue_num}\n\n{engine_used} returned success but did not commit. Auto-salvaging unstaged changes for human review.\n\n{stat[:1500]}",
                    ],
                    cwd=str(wt),
                    timeout=30,
                )
                run(["git", "push", "-u", "origin", branch], cwd=str(wt), timeout=60)
                body_file = Path(f"/tmp/{AGENT}-wip-{issue_num}.md")
                body_file.write_text(f"""## DRAFT - WIP PR auto-salvaged from incomplete {AGENT.title()} run

{AGENT.title()}'s `{engine_used}` run returned success but did not produce a commit. Inspecting the worktree found unstaged changes - committing them here for human review.

Issue: #{issue_num}
Engine: {engine_used}
Turns: {result.num_turns}
Cost equivalent: ${result.cost_usd:.2f}

```
{stat}
```

**Do not merge as-is.** This is incomplete work. Either:
1. Manually finish the implementation on branch `{branch}` and re-open as a proper PR
2. Or close + delete the branch and let {AGENT.title()} retry on a fresh worktree (after splitting the issue if it was too big)

Generated by Alfred OS
""")
                pr_url = gh_pr_create(
                    repo,
                    title=f"DRAFT: WIP partial implementation of #{issue_num}",
                    body_file=body_file,
                    head=branch,
                    labels=["agent:authored", "do-not-review"],
                )
                release_wip_salvage(repo, issue_num, events.firing_id, pr_url)
                remove_worktree(repo, wt)
                spend.increment(failures_today=1, consecutive_failures=1)
                msg = f"⚠️ {AGENT.title()} #{issue_num} salvaged as WIP draft: {pr_url or 'PR open failed'} (turns={result.num_turns})"
                print(msg)
                slack_post(msg, severity="warn")
                return 0
            release_issue(
                repo, issue_num, codename=AGENT, firing_id=events.firing_id, outcome="no-commit"
            )
            remove_worktree(repo, wt)
            spend.increment(failures_today=1, consecutive_failures=1)
            msg = f"[{AGENT.upper()}-NO-COMMIT] {engine_used} success but no commit AND no unstaged changes. #{issue_num}, turns={result.num_turns}. {short(result.result_text, 300)}"
            print(msg)
            slack_post(msg, severity="warn")
            return 0

        # Push + open PR
        run(["git", "push", "-u", "origin", branch], cwd=str(wt), timeout=60)
        commit_subject = run(
            ["git", "log", "-1", "--format=%s"], cwd=str(wt), timeout=10
        ).stdout.strip()
        commit_body = run(
            ["git", "log", "origin/main..HEAD", "--format=%B"], cwd=str(wt), timeout=10
        ).stdout.strip()

        body_file = Path(f"/tmp/{AGENT}-prbody-{issue_num}.md")
        body_file.write_text(f"""## Summary
{commit_body[:2000]}

Closes #{issue_num}

## Test plan
- [ ] CI passes (lint, type-check, build, tests)
- [ ] Reviewer feedback addressed

## {AGENT.title()} meta
- engine: {engine_used}
- turns: {result.num_turns}
- attempt: {next_attempt}

Generated by Alfred OS
""")

        pr_url = gh_pr_create(
            repo, title=commit_subject, body_file=body_file, head=branch, labels=["agent:authored"]
        )
        remove_worktree(repo, wt)

        if pr_url:
            # Transition state machine: agent:in-flight -> agent:pr-open.
            # Also set <agent>-pr-open for back-compat with dashboards/scripts
            # that grep by codename.
            gh_issue_edit(repo, issue_num, add_labels=[f"{AGENT}-pr-open"])
            release_issue(
                repo,
                issue_num,
                codename=AGENT,
                firing_id=events.firing_id,
                outcome="success",
                transition_to="agent:pr-open",
                pr_url=pr_url,
            )
            spend.set(consecutive_failures=0)
            spend.increment(successes_today=1)
            events.emit(
                "pr_opened",
                url=pr_url,
                issue=f"{GH_ORG}/{repo}#{issue_num}",
                turns=result.num_turns,
                cost_usd=result.cost_usd,
            )
            msg = f"✅ {AGENT.title()} shipped: {pr_url} (closes #{issue_num}, turns={result.num_turns})"
            print(msg)
            slack_post(msg)
        else:
            release_issue(
                repo,
                issue_num,
                codename=AGENT,
                firing_id=events.firing_id,
                outcome="pr-create-failed",
            )
            spend.increment(failures_today=1, consecutive_failures=1)
            msg = f"[{AGENT.upper()}-PR-FAILED] commit landed but PR creation failed. #{issue_num}, branch={branch}"
            print(msg)
            slack_post(msg, severity="warn")
        return 0

    if result.subtype == "error_max_turns":
        new_commits = run(
            ["git", "rev-list", "origin/main..HEAD"], cwd=str(wt), timeout=10
        ).stdout.strip()
        commit_count = len([lbl for lbl in new_commits.splitlines() if lbl.strip()])
        gh_issue_comment(
            repo,
            issue_num,
            f"{AGENT.title()}: hit {result.num_turns}-turn cap with {commit_count} commits. Will retry next firing.",
        )
        # Release the claim so next firing can re-pick the issue.
        release_issue(
            repo, issue_num, codename=AGENT, firing_id=events.firing_id, outcome="max-turns"
        )
        remove_worktree(repo, wt)
        # Don't count as failure (resume is the plan)
        msg = f"⏸️ {AGENT.title()} #{issue_num} hit max-turns ({result.num_turns}). Will retry."
        print(msg)
        slack_post(msg)
        return 0

    if result.subtype in ("error_budget", "error_rate_limit"):
        until = None
        if engine_used == "claude":
            until = set_global_block(hours=1, reason=f"{AGENT}-{result.subtype}")
        release_issue(
            repo, issue_num, codename=AGENT, firing_id=events.firing_id, outcome="rate-limit"
        )
        spend.increment(failures_today=1, consecutive_failures=1)
        remove_worktree(repo, wt)
        if until:
            msg = (
                f"{AGENT.title()} hit Claude provider rate limit ({result.subtype}). "
                f"Set global block until {until} - Claude agents will skip until then."
            )
        else:
            msg = (
                f"{AGENT.title()} hit provider rate limit ({result.subtype}, engine={engine_used}); "
                "Claude agents are not globally blocked."
            )
        print(msg)
        slack_post(msg, severity="alert")
        return 0

    # Other failure (transient API rate limit etc.)
    release_issue(
        repo,
        issue_num,
        codename=AGENT,
        firing_id=events.firing_id,
        outcome=f"failure-{result.subtype}",
    )
    spend.increment(failures_today=1, consecutive_failures=1)
    remove_worktree(repo, wt)
    msg = f"❌ {AGENT.title()} #{issue_num}: engine={engine_used} subtype={result.subtype} turns={result.num_turns}. {short(result.result_text, 300)}"
    print(msg)
    slack_post(msg, severity="warn")
    return 0


if __name__ == "__main__":
    sys.exit(main())
