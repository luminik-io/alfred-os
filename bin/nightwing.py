#!/usr/bin/env python3
"""Nightwing - review-to-fix agent. Lands fixes for unresolved P0/P1 reviewer comments.

Per-repo pre-push commands load from ${HOME}/.alfredrc.d/<codename>.yaml
(same format as lucius). Without that file, language-suffix defaults apply.

Reviewer comment matching: bot reviewers (CodeRabbit, Codex/ChatGPT, any
"[bot]" login) are detected by login. The prose-style review agent (default:
Ras al Ghul) is detected by the body prefix "<reviewer-codename>.title()" — set
ALFRED_NIGHTWING_REVIEW_AGENT to match the codename your review agent uses.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")) + "/lib")
from agent_runner import (
    GH_ORG,
    STATE_ROOT,
    WORKSPACE,
    EventLog,
    PreflightFailed,
    PreflightSpec,
    SpendState,
    agent_engine,
    claude_invoke_streaming,
    codex_invoke,
    codex_sandbox_for_agent,
    commit_trailer,
    doctor_mode,
    engine_preflight_bins,
    gh_json,
    gh_pr_comment,
    invoke_agent_engine,
    is_globally_blocked,
    is_repo_paused,
    make_worktree_from_branch,
    maybe_set_global_block_for_result,
    optional_env_int,
    preflight,
    remove_worktree,
    run,
    short,
    slack_post,
    with_lock,
)

AGENT = os.environ.get("AGENT_CODENAME", "nightwing")
NIGHTWING_ENGINE = agent_engine(AGENT, default="hybrid")
LAUNCHD_LABEL = os.environ.get("LAUNCHD_LABEL", f"my.fleet.{AGENT}")
REVIEW_AGENT_NAME = os.environ.get("ALFRED_NIGHTWING_REVIEW_AGENT", "rasalghul").title()

PREFLIGHT = PreflightSpec(
    agent=AGENT,
    bins=[*engine_preflight_bins(NIGHTWING_ENGINE), "gh", "git"],
    require_gh_auth=True,
)
WATCH_REPOS = [
    r.strip() for r in os.environ.get("ALFRED_NIGHTWING_REPOS", "").split(",") if r.strip()
]

DAILY_TURN_CAP = int(os.environ.get("ALFRED_NIGHTWING_TURN_CAP", "600"))

# Persist comment IDs we've fixed (the issue-comments endpoint dedup loop
# can't see issue-comment replies, so persist to disk too).
FIXED_COMMENT_IDS_FILE = STATE_ROOT / AGENT / "fixed-comment-ids.json"


def load_fixed_ids() -> set:
    if not FIXED_COMMENT_IDS_FILE.exists():
        return set()
    try:
        import json

        return set(json.loads(FIXED_COMMENT_IDS_FILE.read_text()))
    except (json.JSONDecodeError, ValueError):
        return set()


def save_fixed_ids(ids: set) -> None:
    import json

    FIXED_COMMENT_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    FIXED_COMMENT_IDS_FILE.write_text(json.dumps(sorted(ids)))


SECURITY_KEYWORDS = re.compile(
    r"\b(auth|secret|token|sql injection|xss|csrf|ssrf|password|oauth|injection|sanitiz)",
    re.IGNORECASE,
)
P0P1_KEYWORDS = re.compile(r"\b(P0|P1|blocking|must fix|critical|🛑|⛔)", re.IGNORECASE)
ALREADY_FIXED = re.compile(rf"{AGENT}.*fixed in", re.IGNORECASE)


def _load_pre_push_config(agent_codename: str) -> dict[str, str]:
    """Same format / defaults as lucius._load_pre_push_config."""
    cfg_path = Path(os.path.expanduser(f"~/.alfredrc.d/{agent_codename}.yaml"))
    user_cfg: dict[str, str] = {}
    if cfg_path.exists():
        try:
            data = tomllib.loads(cfg_path.read_text())
            user_cfg = dict(data.get("pre_push", {}) or {})
        except (OSError, tomllib.TOMLDecodeError):
            user_cfg = {}

    out: dict[str, str] = {}
    for repo in WATCH_REPOS:
        if repo in user_cfg:
            out[repo] = user_cfg[repo]
            continue
        if repo.endswith("-backend") or repo.endswith("-api"):
            out[repo] = "./gradlew check"
        elif repo.endswith(("-frontend", "-mobile", "-web", "-nango")):
            out[repo] = "npm run lint && npx tsc --noEmit"
        elif (WORKSPACE / repo / "pyproject.toml").exists():
            out[repo] = "uv run ruff check . && uv run mypy . && uv run pytest"
        else:
            out[repo] = ""
    return out


PRE_PUSH = _load_pre_push_config(AGENT)


def pick_target(fixed_ids: set) -> tuple[str, dict, list[dict]] | tuple[None, None, None]:
    """Find an agent:authored PR with unresolved P0/P1 reviewer comments.

    Dedup: a comment is "already fixed" if (a) we have its id in the
    on-disk ledger, OR (b) the comment body itself contains
    '<agent> fixed in' (covers manual back-edits).
    """
    review_agent_prefix = REVIEW_AGENT_NAME
    for repo in WATCH_REPOS:
        if is_repo_paused(repo):
            continue
        prs = gh_json(
            [
                "gh",
                "pr",
                "list",
                "-R",
                f"{GH_ORG}/{repo}",
                "--state",
                "open",
                "--label",
                "agent:authored",
                "--json",
                "number,headRefName,reviewDecision,createdAt",
                "--limit",
                "30",
            ],
            default=[],
        )
        if not prs:
            continue
        prs.sort(key=lambda p: p["createdAt"])
        for pr in prs:
            if pr.get("reviewDecision") == "CHANGES_REQUESTED":
                continue  # human owns it
            inline_comments = gh_json(
                [
                    "gh",
                    "api",
                    f"/repos/{GH_ORG}/{repo}/pulls/{pr['number']}/comments",
                    "--paginate",
                ],
                default=[],
            )
            issue_comments = gh_json(
                [
                    "gh",
                    "api",
                    f"/repos/{GH_ORG}/{repo}/issues/{pr['number']}/comments",
                    "--paginate",
                ],
                default=[],
            )
            comments = list(inline_comments) + list(issue_comments)
            unresolved = []
            for c in comments:
                user = (c.get("user") or {}).get("login", "")
                body = c.get("body", "")
                is_bot_reviewer = (
                    user == "coderabbitai[bot]"
                    or "codex" in user.lower()
                    or "chatgpt" in user.lower()
                    or user.endswith("[bot]")
                )
                # The prose-style review agent posts via gh pr comment from
                # a human GitHub identity; identify by body prefix.
                is_review_agent = (
                    body.startswith(f"{review_agent_prefix} - review")
                    or body.startswith(f"{review_agent_prefix} P0:")
                    or body.startswith(f"{review_agent_prefix} P1:")
                )
                if not (is_bot_reviewer or is_review_agent):
                    continue
                if not P0P1_KEYWORDS.search(body):
                    continue
                if ALREADY_FIXED.search(body):
                    continue
                cid = c.get("id")
                if cid in fixed_ids:
                    continue
                unresolved.append(
                    {
                        "id": cid,
                        "path": c.get("path", ""),
                        "line": c.get("line"),
                        "body": body,
                        "user": user,
                    }
                )
            if unresolved:
                return repo, pr, unresolved[:3]  # max 3 per firing
    return None, None, None


def build_prompt(
    repo: str,
    pr_num: int,
    cpath: str,
    cline,
    cuser: str,
    cbody: str,
    wt,
    pre_push: str,
    firing_id: str,
) -> str:
    trailer = commit_trailer(
        AGENT,
        firing_id,
        extra={"pr": f"{GH_ORG}/{repo}#{pr_num}"},
    )
    return f"""You are {AGENT.title()}, fixing a single review comment on a PR. Apply ONLY the change requested. No refactors, no opportunistic cleanup.

PR: https://github.com/{GH_ORG}/{repo}/pull/{pr_num}
File: {cpath}
Line: {cline}
Reviewer: {cuser}

Comment body verbatim:
{cbody}

Working directory: {wt}

Constraints:
- Touch only {cpath} unless the comment explicitly asks for a multi-file change.
- Pre-push checks must pass before you commit: {pre_push}
- No em-dashes anywhere. No "unlock", "leverage", "seamless", "transform". No fabricated numbers.
- Surgical edit. Read the file first.

When done:
1. Stage your edit.
2. Commit with conventional-commit message: fix(<scope>): address {cuser} comment - <one-line summary>
3. The commit message body MUST end with this exact trailer block (blank line before it, no quoting, no rewording):

{trailer}

4. Print: "[OK] commit <sha> - <one-line>"

The trailer is a forensic anchor. `git log --grep "Agent-Firing-Id: {firing_id}"` should find this commit.

If you cannot resolve cleanly:
- Print: "[BLOCKED] <reason>"
"""


def main() -> int:
    with_lock(AGENT)

    try:
        preflight(PREFLIGHT)
    except PreflightFailed:
        return 0

    if doctor_mode():
        print(f"[{AGENT.upper()}-DOCTOR-OK]")
        return 0

    if not WATCH_REPOS:
        print(f"[{AGENT.upper()}-IDLE] no repos configured (set ALFRED_NIGHTWING_REPOS)")
        return 0

    events = EventLog(agent=AGENT)
    events.emit("firing_started")

    blocked = is_globally_blocked()
    if blocked:
        print(f"[{AGENT.upper()}-GLOBAL-BLOCKED] {blocked}. Skipping firing.")
        events.emit("firing_complete", outcome="global-blocked")
        return 0
    spend = SpendState(AGENT)

    if spend.state["turns_today"] >= DAILY_TURN_CAP:
        msg = f"[{AGENT.upper()}-DAILY-CAP] turns={spend.state['turns_today']} >= {DAILY_TURN_CAP}. Pausing."
        print(msg)
        slack_post(msg)
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], timeout=10)
        events.emit("firing_complete", outcome="daily-cap")
        return 0

    fixed_ids = load_fixed_ids()
    repo, pr, comments = pick_target(fixed_ids)
    if not repo:
        print(f"[{AGENT.upper()}-IDLE]")
        events.emit("firing_complete", outcome="idle-no-comments")
        return 0

    pr_num = pr["number"]
    head_ref = pr["headRefName"]
    events.emit("pr_picked", repo=f"{GH_ORG}/{repo}", number=pr_num, comment_count=len(comments))

    # Worktree at the PR branch
    try:
        wt = make_worktree_from_branch(repo, AGENT, head_ref, str(pr_num))
    except RuntimeError as e:
        msg = f"[{AGENT.upper()}-ERROR] {e}"
        print(msg)
        slack_post(msg)
        events.emit("firing_complete", outcome="worktree-error")
        return 0

    fixes_landed = 0
    fix_summary = []
    total_turns = 0
    engine_counts: dict[str, int] = {}

    for c in comments:
        cbody = c["body"]
        cpath = c["path"]
        cline = c["line"]
        cuser = c["user"]
        cid = c["id"]

        # Security gate: surface to operator instead of auto-fixing
        if SECURITY_KEYWORDS.search(cbody):
            slack_post(
                f"⛔ {AGENT.title()} P0 security flag - manual review needed: comment {cid} "
                f"on PR {pr_num} ({cuser}, {cpath}:{cline})"
            )
            continue

        prompt = build_prompt(
            repo,
            pr_num,
            cpath,
            cline,
            cuser,
            cbody,
            wt,
            PRE_PUSH.get(repo, ""),
            firing_id=events.firing_id,
        )

        def _on_engine_fallback(fallback_result):
            events.emit(
                "llm_fallback",
                from_engine="claude",
                to_engine="codex",
                reason=fallback_result.error_message or fallback_result.result_text,
            )

        result, engine_used = invoke_agent_engine(
            prompt,
            engine=NIGHTWING_ENGINE,
            claude_fn=claude_invoke_streaming,
            codex_fn=codex_invoke,
            workdir=wt,
            claude_allowed_tools="Read,Edit,Bash,Grep",
            agent=AGENT,
            firing_id=events.firing_id,
            claude_max_turns=optional_env_int("ALFRED_NIGHTWING_MAX_TURNS", minimum=25),
            timeout=600,
            codex_timeout=600,
            codex_sandbox=codex_sandbox_for_agent(AGENT, default="workspace-write"),
            codex_bypass_approvals_and_sandbox=True,
            on_fallback=_on_engine_fallback,
        )
        total_turns += result.num_turns
        engine_counts[engine_used] = engine_counts.get(engine_used, 0) + 1
        events.emit(
            "llm_invoke_done",
            engine=engine_used,
            comment_id=cid,
            turns=result.num_turns,
            subtype=result.subtype,
            success=result.success,
        )

        if not result.success:
            until = maybe_set_global_block_for_result(AGENT, result, engine_used=engine_used)
            if until:
                msg = (
                    f"{AGENT.title()} hit provider rate limit ({result.subtype}, engine={engine_used}). "
                    f"Global block until {until}."
                )
                print(msg)
                slack_post(msg, severity="alert")
                events.emit("firing_complete", outcome=f"llm-{result.subtype}", engine=engine_used)
                remove_worktree(repo, wt)
                return 0
            print(
                f"[{AGENT.upper()}-FAIL] comment {cid}: engine={engine_used} subtype={result.subtype} turns={result.num_turns}"
            )
            continue

        # Verify a commit landed
        log = run(["git", "log", "-1", "--format=%H"], cwd=str(wt), timeout=10)
        new_sha = log.stdout.strip()
        parent_sha = run(
            ["git", "log", "-1", "--format=%H", f"origin/{head_ref}"],
            cwd=str(wt),
            timeout=10,
        ).stdout.strip()
        if not new_sha or new_sha == parent_sha:
            print(
                f"[{AGENT.upper()}-NO-COMMIT] comment {cid}: {engine_used} said success but no new commit"
            )
            continue

        # Push + reply on the PR
        push = run(["git", "push", "origin", f"HEAD:{head_ref}"], cwd=str(wt), timeout=60)
        if push.returncode != 0:
            print(f"[{AGENT.upper()}-PUSH-FAIL] comment {cid}: {short(push.stderr, 200)}")
            continue
        gh_pr_comment(
            repo,
            pr_num,
            f"{AGENT.title()}: fixed in {new_sha[:7]} (re: comment {cid} from {cuser})",
        )
        events.emit("fix_pushed", comment_id=cid, commit_sha=new_sha[:7], reviewer=cuser)
        fixes_landed += 1
        fix_summary.append(f"- {new_sha[:7]}: {cuser} comment {cid}")
        # Persist so the next firing's pick_target() skips this comment
        fixed_ids.add(cid)

    save_fixed_ids(fixed_ids)
    spend.increment(firings_today=1, turns_today=total_turns)
    remove_worktree(repo, wt)
    engine_summary = (
        ", ".join(f"{engine}:{count}" for engine, count in sorted(engine_counts.items())) or "none"
    )

    if fixes_landed == 0:
        # No fixes landed but the firing completed cleanly. Count as no-op
        # success so success-rate metrics don't show a misleading 0%.
        spend.increment(successes_today=1)
        msg = f"{AGENT.title()}: no fixes landed on PR {pr_num} (engines={engine_summary}, turns={total_turns}, candidates={len(comments)})"
        print(msg)
        events.emit("firing_complete", outcome="no-fixes-landed", candidates=len(comments))
        return 0

    spend.increment(fixes_landed=fixes_landed, successes_today=1)
    msg = (
        f"✅ {AGENT.title()}: cleared {fixes_landed} comment(s) on "
        f"https://github.com/{GH_ORG}/{repo}/pull/{pr_num} "
        f"(engines={engine_summary}, turns={total_turns})\n" + "\n".join(fix_summary)
    )
    print(msg)
    slack_post(msg)
    events.emit("firing_complete", outcome="fixes-landed", count=fixes_landed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
