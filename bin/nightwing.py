#!/usr/bin/env python3
"""Nightwing - review-to-fix agent. Lands fixes for unresolved P0/P1 reviewer comments.

Per-repo pre-push commands load from ${HOME}/.alfredrc.d/<codename>.yaml
(same format as lucius). Without that file, language-suffix defaults apply.

Reviewer comment matching: bot reviewers (CodeRabbit, Codex/ChatGPT, any
"[bot]" login) are detected by login. The prose-style review agent (default:
Ras al Ghul) is detected by the body prefix "<reviewer-codename>.title()", set
ALFRED_NIGHTWING_REVIEW_AGENT to match the codename your review agent uses.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(
    0,
    (os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")) + "/lib",
)
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
    create_recovery_ref,
    doctor_mode,
    engine_preflight_bins,
    gh_json,
    gh_pr_comment,
    invoke_agent_engine,
    is_globally_blocked,
    is_repo_paused,
    local_repo_dir,
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
from agent_runner.transcripts import transcript_path
from workflow_validation import validate_changed_workflows

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

# Per-(PR, comment) consecutive-no-commit streak counter. After N misses
# in a row Nightwing stops retrying that comment and escalates with a
# Slack post + `nightwing:human-needed` label. The operator clears the
# state by adding `nightwing:reset` to the PR or by deleting the entry
# from this file. See issue #109.
NO_COMMIT_STREAKS_FILE = STATE_ROOT / AGENT / "no-commit-streaks.json"
NO_COMMIT_ESCALATE_AFTER = int(os.environ.get("ALFRED_NIGHTWING_ESCALATE_AFTER", "3"))
ESCALATE_LABEL = "nightwing:human-needed"
RESET_LABEL = "nightwing:reset"


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


def _streak_key(repo: str, pr_num: int, comment_id) -> str:
    """Key shape mirrors the issue's recommendation: (pr_url, comment_id)."""
    return f"{GH_ORG}/{repo}#{pr_num}:{comment_id}"


def load_no_commit_streaks() -> dict[str, int]:
    if not NO_COMMIT_STREAKS_FILE.exists():
        return {}
    try:
        import json

        data = json.loads(NO_COMMIT_STREAKS_FILE.read_text())
        return {str(k): int(v) for k, v in data.items() if isinstance(v, int) and v >= 0}
    except (OSError, ValueError, TypeError):
        return {}


def save_no_commit_streaks(streaks: dict[str, int]) -> None:
    import json

    NO_COMMIT_STREAKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    NO_COMMIT_STREAKS_FILE.write_text(json.dumps(streaks, sort_keys=True))


def diagnose_no_commit(wt: str, debug_dir: str | None) -> str:
    """Build the operator-facing diagnostic appended to [NIGHTWING-NO-COMMIT].

    Two pieces:
      1. ``git status --porcelain`` so the operator sees whether the engine
         wrote files (and just didn't commit) vs. wrote nothing at all.
      2. Pointer to the per-firing transcript dump (when one is on disk)
         so the operator can grep for what Claude actually said.

    Both are best-effort; failures fall back to a short note so the
    NO-COMMIT log line is never empty.
    """
    lines: list[str] = []
    try:
        status = run(["git", "status", "--porcelain"], cwd=str(wt), timeout=10).stdout.strip()
    except Exception:
        status = ""
    if status:
        lines.append("  Working tree (git status --porcelain):")
        for line in status.splitlines()[:20]:
            lines.append(f"    {line}")
        lines.append(
            "  -> Possible cause: engine wrote files but did not commit; "
            "check pre-commit hooks and the transcript for a missing `git commit`."
        )
    else:
        lines.append("  Working tree: clean (no staged or unstaged changes).")
        lines.append(
            "  -> Possible cause: engine described the fix in prose without invoking "
            "a write tool. Tighten the prompt to require an actual edit + commit."
        )
    if debug_dir:
        lines.append(f"  Transcript: {debug_dir}")
    return "\n".join(lines)


def escalate_no_commit(repo: str, pr_num: int, comment_id, streak: int) -> None:
    """After N consecutive no-commits on the same (PR, comment) tuple,
    add the ``nightwing:human-needed`` label and post Slack once.

    The label is the out-of-band signal for the operator queue; the
    Slack post is the heads-up. Label-add failures are non-fatal so a
    label-permission misconfig doesn't kill the firing.
    """
    msg = (
        f"{AGENT.title()}: {streak} consecutive no-commits on "
        f"{GH_ORG}/{repo}#{pr_num} comment {comment_id}. Operator action needed "
        f"(likely an LLM hallucination or hook config issue). Marked "
        f"`{ESCALATE_LABEL}`; add `{RESET_LABEL}` after fixing to retry."
    )
    print(f"[{AGENT.upper()}-ESCALATE] {msg}")
    try:
        slack_post(msg, severity="alert")
    except Exception as exc:
        print(f"[{AGENT.upper()}-ESCALATE-SLACK-FAIL] {exc}")
    try:
        run(
            [
                "gh",
                "pr",
                "edit",
                str(pr_num),
                "--repo",
                f"{GH_ORG}/{repo}",
                "--add-label",
                ESCALATE_LABEL,
            ],
            timeout=30,
        )
    except Exception as exc:
        print(f"[{AGENT.upper()}-ESCALATE-LABEL-FAIL] {exc}")


def reset_label_present(repo: str, pr_num: int) -> bool:
    """Operator-controlled reset: if the PR carries ``nightwing:reset``,
    drop the streak entries for that PR and remove the label so the
    next firing starts clean."""
    try:
        cp = run(
            [
                "gh",
                "pr",
                "view",
                str(pr_num),
                "--repo",
                f"{GH_ORG}/{repo}",
                "--json",
                "labels",
                "--jq",
                ".labels[].name",
            ],
            timeout=15,
        )
    except Exception:
        return False
    names = {line.strip() for line in cp.stdout.splitlines() if line.strip()}
    return RESET_LABEL in names


SECURITY_KEYWORDS = re.compile(
    r"\b(auth|secret|token|sql injection|xss|csrf|ssrf|password|oauth|injection|sanitiz)",
    re.IGNORECASE,
)
ALREADY_FIXED = re.compile(rf"{AGENT}.*fixed in", re.IGNORECASE)
FIXED_REPLY_COMMENT_ID = re.compile(
    rf"{AGENT}:\s*fixed in\s+[0-9a-f]{{7,40}}\s*\(re:\s*comment\s+(\d+)\b",
    re.IGNORECASE,
)


def _extract_markdown_section(body: str, heading_pattern: str) -> str:
    match = re.search(rf"^##\s+{heading_pattern}.*$", body, re.IGNORECASE | re.MULTILINE)
    if not match:
        return ""
    rest = body[match.end() :]
    next_heading = re.search(r"^##\s+", rest, re.MULTILINE)
    return rest[: next_heading.start()] if next_heading else rest


def _section_has_finding(section: str) -> bool:
    for line in section.splitlines():
        text = line.strip()
        if not text.startswith(("-", "*")):
            continue
        item = text.lstrip("-* ").strip().lower().rstrip(".")
        if item and item not in {"none", "n/a", "na"}:
            return True
    return False


def comment_severity(body: str) -> str | None:
    """Classify reviewer comment severity without treating "P0: None" as P0."""
    stripped = body.lstrip()
    review_agent = re.escape(REVIEW_AGENT_NAME)
    if re.match(rf"^{review_agent}\s+P0:", stripped, re.IGNORECASE):
        return "P0"
    if re.match(rf"^{review_agent}\s+P1:", stripped, re.IGNORECASE):
        return "P1"

    p0_section = _extract_markdown_section(body, r"Blockers\s+\(P0\)")
    if p0_section:
        if _section_has_finding(p0_section):
            return "P0"
        p1_section = _extract_markdown_section(body, r"Should fix before merge\s+\(P1\)")
        if p1_section and _section_has_finding(p1_section):
            return "P1"
        return None

    if re.search(r"\b(P0|critical|blocking|must fix|🛑|⛔)", body, re.IGNORECASE):
        return "P0"
    if re.search(r"\bP1\b", body, re.IGNORECASE):
        return "P1"
    return None


def fixed_comment_ids_from_pr_comments(comments: list[dict]) -> set[int]:
    """Return review comment IDs mentioned in prior Nightwing success replies."""
    fixed: set[int] = set()
    for comment in comments:
        body = comment.get("body", "") if isinstance(comment, dict) else ""
        if not isinstance(body, str):
            continue
        for match in FIXED_REPLY_COMMENT_ID.finditer(body):
            try:
                fixed.add(int(match.group(1)))
            except ValueError:
                continue
    return fixed


def preserve_workflow_validation_failure(
    wt: Path,
    *,
    head_ref: str,
    pr_num: int,
    comment_id: int,
    workflow_validation,
    events: EventLog,
) -> tuple[str, str | None]:
    """Warn and create a recovery ref for unpushed review-fix workflow changes."""
    recovery_ref = create_recovery_ref(wt, branch=head_ref)
    detail = short(
        workflow_validation.stderr
        or workflow_validation.stdout
        or workflow_validation.reason
        or "workflow validation failed",
        260,
    )
    files = ", ".join(workflow_validation.files) or "(unknown workflow)"
    ref_part = f"; recovery_ref={recovery_ref}" if recovery_ref else ""
    reason = (
        f"workflow validation failed for comment {comment_id}; files={files}{ref_part}. {detail}"
    )
    print(
        f"[{AGENT.upper()}-WORKFLOW-VALIDATION-FAILED] comment {comment_id}: "
        f"files={files}{ref_part}. {detail}"
    )
    slack_post(
        f"[{AGENT.upper()}-WORKFLOW-VALIDATION-FAILED] PR {pr_num} "
        f"comment {comment_id}; files={files}{ref_part}. {detail}",
        severity="warn",
    )
    events.emit(
        "workflow_validation_failed",
        comment_id=comment_id,
        files=list(workflow_validation.files),
        recovery_ref=recovery_ref,
    )
    return reason, recovery_ref


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
        elif (WORKSPACE / local_repo_dir(repo) / "pyproject.toml").exists():
            out[repo] = "uv run ruff check . && uv run mypy . && uv run pytest"
        else:
            out[repo] = ""
    return out


PRE_PUSH = _load_pre_push_config(AGENT)


def _refresh_pre_push_config() -> None:
    """Reload inferred pre-push commands after preflight syncs checkouts."""
    global PRE_PUSH
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
                "number,headRefName,reviewDecision,createdAt,labels",
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
            # Escalation handoff (PR #111 follow-up): when an earlier
            # firing tripped the no-commit escalation threshold and
            # added `nightwing:human-needed`, the operator owns the PR
            # until they add `nightwing:reset`. Without this skip, the
            # next `pick_target()` call selects the same comment and
            # Nightwing burns turns retrying the very case escalation
            # was meant to stop. The `nightwing:reset` label is the
            # operator's "try again" signal; when both labels are
            # present, the PR re-enters the pool and the inner
            # reset-handler clears the state.
            label_names = {
                (label.get("name") or "")
                for label in pr.get("labels") or []
                if isinstance(label, dict)
            }
            if ESCALATE_LABEL in label_names and RESET_LABEL not in label_names:
                continue
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
            fixed_ids_for_pr = set(fixed_ids) | fixed_comment_ids_from_pr_comments(issue_comments)
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
                severity = comment_severity(body)
                if not severity:
                    continue
                if ALREADY_FIXED.search(body):
                    continue
                cid = c.get("id")
                if cid in fixed_ids_for_pr:
                    continue
                unresolved.append(
                    {
                        "id": cid,
                        "path": c.get("path", ""),
                        "line": c.get("line"),
                        "body": body,
                        "user": user,
                        "severity": severity,
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
    _refresh_pre_push_config()

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

    # Per-(PR, comment) no-commit streak counter (issue #109). Loaded
    # here so a single firing can both bump streaks and detect operator
    # reset via the `nightwing:reset` label.
    no_commit_streaks = load_no_commit_streaks()
    if reset_label_present(repo, pr_num):
        prefix = f"{GH_ORG}/{repo}#{pr_num}:"
        cleared = [k for k in list(no_commit_streaks) if k.startswith(prefix)]
        for k in cleared:
            no_commit_streaks.pop(k, None)
        if cleared:
            print(
                f"[{AGENT.upper()}-RESET] operator added `{RESET_LABEL}` to "
                f"{GH_ORG}/{repo}#{pr_num}; cleared {len(cleared)} streak entry(ies)."
            )
            save_no_commit_streaks(no_commit_streaks)
        # Best-effort label cleanup so the next firing doesn't re-reset.
        # Also drop `nightwing:human-needed` if it was set by a prior
        # escalation: pick_target gated on its presence (PR #111
        # follow-up), so leaving it on would re-block this PR after
        # the operator's explicit reset.
        with contextlib.suppress(Exception):
            run(
                [
                    "gh",
                    "pr",
                    "edit",
                    str(pr_num),
                    "--repo",
                    f"{GH_ORG}/{repo}",
                    "--remove-label",
                    RESET_LABEL,
                    "--remove-label",
                    ESCALATE_LABEL,
                ],
                timeout=15,
            )

    # Worktree at the PR branch
    try:
        wt = make_worktree_from_branch(local_repo_dir(repo), AGENT, head_ref, str(pr_num))
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
    preserved_failure_reason = ""
    preserved_recovery_ref: str | None = None

    for c in comments:
        cbody = c["body"]
        cpath = c["path"]
        cline = c["line"]
        cuser = c["user"]
        cid = c["id"]
        severity = c.get("severity") or comment_severity(cbody) or "P1"

        # Security gate: true P0 security findings need manual review. P1
        # comments may mention secrets or auth as context and should still be
        # eligible for the surgical auto-fix path.
        if severity == "P0" and SECURITY_KEYWORDS.search(cbody):
            # PR-level (conversation) review comments have no `path` /
            # `line` (the GitHub API returns null for both). Without
            # this guard, the Slack message renders `(user, :None)`
            # for every PR-level flag, which is what operators saw on
            # the production fleet. Render a clean `PR-level review`
            # marker instead so the alert is actionable on first read.
            location = f"{cpath}:{cline}" if cpath and cline is not None else "PR-level review"
            slack_post(
                f"⛔ {AGENT.title()} {severity} security flag - manual review needed: comment {cid} "
                f"on PR {pr_num} ({cuser}, {location})"
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
            memory_repo=f"{GH_ORG}/{repo}" if GH_ORG else repo,
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
            # Diagnostic upgrade (issue #109): include git status and the
            # transcript path so the operator can tell which of the
            # five no-commit failure modes happened without grepping the
            # firing log themselves.
            try:
                transcript = str(transcript_path(AGENT, events.firing_id))
            except Exception:
                transcript = None
            diag = diagnose_no_commit(str(wt), transcript)
            print(
                f"[{AGENT.upper()}-NO-COMMIT] comment {cid}: {engine_used} exited 0 "
                f"but HEAD did not advance.\n{diag}"
            )
            issue_comments = gh_json(
                [
                    "gh",
                    "api",
                    f"/repos/{GH_ORG}/{repo}/issues/{pr_num}/comments",
                    "--paginate",
                ],
                default=[],
            )
            if cid in fixed_comment_ids_from_pr_comments(issue_comments):
                print(
                    f"[{AGENT.upper()}-ALREADY-FIXED] comment {cid}: found prior "
                    f"{AGENT.title()} fixed reply; skipping no-commit streak."
                )
                fixed_ids.add(cid)
                no_commit_streaks.pop(_streak_key(repo, pr_num, cid), None)
                continue
            # Bump the (PR, comment) streak; escalate at the configured
            # threshold so an infinite-retry loop on the same comment
            # surfaces as an operator-actionable Slack post + label.
            key = _streak_key(repo, pr_num, cid)
            streak = no_commit_streaks.get(key, 0) + 1
            no_commit_streaks[key] = streak
            if streak >= NO_COMMIT_ESCALATE_AFTER:
                escalate_no_commit(repo, pr_num, cid, streak)
                # Once escalated, drop the entry so a future
                # nightwing:reset retry can start the streak fresh.
                no_commit_streaks.pop(key, None)
            continue

        # Push + reply on the PR
        workflow_validation = validate_changed_workflows(wt, base="origin/main")
        if not workflow_validation.ok:
            preserved_failure_reason, preserved_recovery_ref = preserve_workflow_validation_failure(
                wt,
                head_ref=head_ref,
                pr_num=pr_num,
                comment_id=cid,
                workflow_validation=workflow_validation,
                events=events,
            )
            break
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
        # A successful fix clears the no-commit streak for this comment
        # so a future regression doesn't escalate prematurely.
        no_commit_streaks.pop(_streak_key(repo, pr_num, cid), None)

    save_fixed_ids(fixed_ids)
    save_no_commit_streaks(no_commit_streaks)
    spend.increment(firings_today=1, turns_today=total_turns)
    engine_summary = (
        ", ".join(f"{engine}:{count}" for engine, count in sorted(engine_counts.items())) or "none"
    )

    if preserved_failure_reason:
        spend.increment(failures_today=1, consecutive_failures=1)
        msg = (
            f"{AGENT.title()}: preserved local worktree for PR {pr_num}; "
            f"{preserved_failure_reason} worktree={wt}"
        )
        print(msg)
        events.emit(
            "firing_complete",
            outcome="workflow-validation-failed",
            preserved_worktree=str(wt),
            recovery_ref=preserved_recovery_ref,
        )
        return 1

    remove_worktree(repo, wt)

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
