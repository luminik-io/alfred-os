#!/usr/bin/env python3
"""Bane - test coverage agent. Adds tests for actively-changed undertested files.

Per-repo configuration (slug, pre-push command, coverage hint) loads from
${HOME}/.alfredrc.d/<codename>.yaml. TOML format:

    repos:
      backend:
        pre_push: ./gradlew :api:test --tests <TARGET_CLASS>
        coverage_hint: parse build/reports/jacoco/jacocoTestReport/jacocoTestReport.xml
      frontend:
        pre_push: npm run test -- <test-path> && npm run lint && npx tsc --noEmit
        coverage_hint: parse coverage/coverage-summary.json

Without that file, the runner falls back to language-suffix defaults (see
_load_pre_push_config in lucius.py for the same pattern). Coverage hint
defaults to "(operator: configure coverage_hint per repo)".
"""

from __future__ import annotations

import os
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
    claude_invoke,
    commit_trailer,
    doctor_mode,
    gh_pr_create,
    is_globally_blocked,
    is_repo_paused,
    make_worktree,
    optional_env_int,
    preflight,
    remove_worktree,
    run,
    slack_post,
    with_lock,
)

AGENT = os.environ.get("AGENT_CODENAME", "bane")
LAUNCHD_LABEL = os.environ.get("LAUNCHD_LABEL", f"my.fleet.{AGENT}")
LAST_REPO_FILE = STATE_ROOT / AGENT / "last-repo.txt"

ROTATION = [r.strip() for r in os.environ.get("ALFRED_BANE_REPOS", "").split(",") if r.strip()]

PREFLIGHT = PreflightSpec(
    agent=AGENT,
    bins=["claude", "gh", "git"],
    require_gh_auth=True,
    require_workspace_repos=ROTATION,
)


def _load_repo_config(agent_codename: str) -> dict[str, dict[str, str]]:
    """Read per-repo pre_push + coverage_hint from
    ${HOME}/.alfredrc.d/<codename>.yaml. See module docstring for format.
    """
    cfg_path = Path(os.path.expanduser(f"~/.alfredrc.d/{agent_codename}.yaml"))
    user_cfg: dict[str, dict[str, str]] = {}
    if cfg_path.exists():
        try:
            data = tomllib.loads(cfg_path.read_text())
            user_cfg = dict(data.get("repos", {}) or {})
        except (OSError, tomllib.TOMLDecodeError):
            user_cfg = {}

    out: dict[str, dict[str, str]] = {}
    for repo in ROTATION:
        entry = dict(user_cfg.get(repo, {}) or {})
        if "pre_push" not in entry:
            if repo.endswith("-backend") or repo.endswith("-api"):
                entry["pre_push"] = "./gradlew check"
            elif repo.endswith(("-frontend", "-mobile", "-web", "-nango")):
                entry["pre_push"] = "npm test && npm run lint"
            elif (WORKSPACE / repo / "pyproject.toml").exists():
                entry["pre_push"] = "uv run pytest && uv run ruff check ."
            else:
                entry["pre_push"] = ""
        if "coverage_hint" not in entry:
            entry["coverage_hint"] = (
                f"(operator: configure coverage_hint per repo in ~/.alfredrc.d/{agent_codename}.yaml)"
            )
        out[repo] = entry
    return out


REPO_CONFIG = _load_repo_config(AGENT)


def pick_repo() -> str | None:
    """Pick the next rotation repo. Skip paused repos. Returns None if every
    rotation slot is paused.
    """
    if not ROTATION:
        return None
    last = ""
    if LAST_REPO_FILE.exists():
        last = LAST_REPO_FILE.read_text().strip()
    try:
        start = (ROTATION.index(last) + 1) % len(ROTATION)
    except ValueError:
        start = 0
    for offset in range(len(ROTATION)):
        idx = (start + offset) % len(ROTATION)
        candidate = ROTATION[idx]
        if is_repo_paused(candidate):
            continue
        LAST_REPO_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_REPO_FILE.write_text(candidate)
        return candidate
    return None


def build_prompt(
    repo: str, wt: Path, repo_claude_md: str, coverage_hint: str, pre_push: str, firing_id: str
) -> str:
    trailer = commit_trailer(
        AGENT,
        firing_id,
        extra={"repo": f"{GH_ORG}/{repo}"},
    )
    return f"""You are {AGENT.title()}, the test coverage agent. Add tests for the actively-changed, undertested file with the lowest line coverage.

Working directory: {wt}
Repo: {GH_ORG}/{repo}

The repo CLAUDE.md (pre-cached):
---
{repo_claude_md}
---

Coverage source: {coverage_hint}

Your selection algorithm:
1. Get coverage data per the hint above.
2. Find files modified in the last 14 days: git log --since=14.days --name-only --pretty=format: | sort -u | grep source files only (no tests, no generated, no build artifacts).
3. Join 14-day-changes with coverage. Rank ascending by line coverage. Take top 3.
4. If all 3 candidates are >= 90% coverage already, print "[BANE-SILENT] all candidates already well-covered" and EXIT WITHOUT COMMITTING.
5. Pick the lowest-coverage candidate. Read it + its existing test file (if any).

Then write tests:
- Only ADD tests. Never modify production code. If a test reveals a real bug, write a one-line bug description and exit without committing.
- Match existing test conventions in the repo (assertion library, mocking, fixture shape, naming).
- Cover 1-3 high-value uncovered branches. Prefer public API + happy path + one error path.
- Never mock a dependency the codebase does not already mock.

Pre-push checks (must pass before commit):
{pre_push}

When done:
1. Stage your test file additions.
2. Commit with conventional-commit message: test(<scope>): add coverage for <Class/Component> - <one-line intent>. Body explains which branches got covered and why.
3. The commit message body MUST end with this exact trailer block (blank line before it, no quoting, no rewording):

{trailer}

4. Print: "[OK] commit <sha> - file=<path> branches=<list>"

The trailer is a forensic anchor. `git log --grep "Agent-Firing-Id: {firing_id}"` should find this commit. Do not modify the codename, firing-id, or repo lines.

If you find a real bug (test red without your changes):
- Print: "[BUG-FOUND] <one-line description>"

If you cannot complete:
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

    if not ROTATION:
        print(f"[{AGENT.upper()}-IDLE] no repos configured (set ALFRED_BANE_REPOS)")
        return 0

    events = EventLog(agent=AGENT)
    events.emit("firing_started")

    blocked = is_globally_blocked()
    if blocked:
        print(f"[{AGENT.upper()}-GLOBAL-BLOCKED] {blocked}. Skipping firing.")
        events.emit("firing_complete", outcome="global-blocked")
        return 0
    spend = SpendState(AGENT)

    repo = pick_repo()
    if repo is None:
        events.emit("firing_complete", outcome="all-repos-paused")
        print(f"[{AGENT.upper()}-IDLE] all rotation repos paused")
        return 0
    cfg = REPO_CONFIG.get(repo, {})
    pre_push = cfg.get("pre_push", "")
    coverage_hint = cfg.get("coverage_hint", "")
    local_path = WORKSPACE / repo
    events.emit("repo_picked", repo=f"{GH_ORG}/{repo}")

    repo_claude_md = ""
    md = local_path / "CLAUDE.md"
    if md.exists():
        repo_claude_md = md.read_text()

    # Worktree
    try:
        wt, branch = make_worktree(repo, AGENT, "coverage")
    except RuntimeError as e:
        msg = f"[{AGENT.upper()}-ERROR] {e}"
        print(msg)
        slack_post(msg)
        events.emit("firing_complete", outcome="worktree-error")
        return 0
    events.emit("worktree_created", branch=branch, path=str(wt))

    prompt = build_prompt(
        repo, wt, repo_claude_md, coverage_hint, pre_push, firing_id=events.firing_id
    )

    result = claude_invoke(
        prompt,
        workdir=wt,
        allowed_tools="Read,Edit,Write,Bash,Grep",
        max_turns=optional_env_int("ALFRED_BANE_MAX_TURNS", minimum=40),
        timeout=1200,
    )
    spend.increment(firings_today=1, turns_today=result.num_turns, cost_usd_today=result.cost_usd)
    events.emit(
        "claude_invoke_done", turns=result.num_turns, subtype=result.subtype, success=result.success
    )

    if not result.success:
        remove_worktree(repo, wt)
        msg = f"❌ {AGENT.title()} {repo}: subtype={result.subtype} turns={result.num_turns}"
        print(msg)
        slack_post(msg)
        events.emit("firing_complete", outcome=f"claude-{result.subtype}")
        return 0

    text = result.result_text or ""
    head = text[:300]

    if "[BANE-SILENT]" in head:
        remove_worktree(repo, wt)
        msg = f"[{AGENT.upper()}-SILENT] {repo}: all candidates well-covered. turns={result.num_turns}"
        print(msg)
        events.emit("firing_complete", outcome="silent-well-covered")
        return 0

    if "[BUG-FOUND]" in head:
        bug_line = next((lbl for lbl in text.splitlines() if "[BUG-FOUND]" in lbl), "")
        bug_desc = bug_line.split("[BUG-FOUND]", 1)[-1].strip()[:200]
        remove_worktree(repo, wt)
        issue_url = ""
        res = run(
            [
                "gh",
                "issue",
                "create",
                "-R",
                f"{GH_ORG}/{repo}",
                "--title",
                f"bug: {bug_desc}",
                "--body",
                f"{AGENT.title()} discovered this while attempting to add tests. Triage required.",
                "--label",
                "bug",
            ],
            timeout=30,
        )
        if res.returncode == 0:
            issue_url = (res.stdout or "").strip().splitlines()[-1]
        msg = f"🐛 {AGENT.title()} {repo}: bug found, filed {issue_url}. turns={result.num_turns}"
        print(msg)
        slack_post(msg)
        events.emit("firing_complete", outcome="bug-found", issue_url=issue_url)
        return 0

    # Verify commit landed
    new_commits = run(
        ["git", "rev-list", "origin/main..HEAD"], cwd=str(wt), timeout=10
    ).stdout.strip()
    if not new_commits:
        remove_worktree(repo, wt)
        msg = f"[{AGENT.upper()}-NO-COMMIT] {repo}: claude success but no commit. turns={result.num_turns}"
        print(msg)
        slack_post(msg)
        events.emit("firing_complete", outcome="no-commit")
        return 0

    # Push + open PR
    run(["git", "push", "-u", "origin", branch], cwd=str(wt), timeout=60)
    commit_subject = run(
        ["git", "log", "-1", "--format=%s"], cwd=str(wt), timeout=10
    ).stdout.strip()
    commit_body = run(
        ["git", "log", "origin/main..HEAD", "--format=%B"], cwd=str(wt), timeout=10
    ).stdout.strip()

    body_file = Path(f"/tmp/{AGENT}-prbody-{repo}.md")
    body_file.write_text(f"""## Summary
{commit_body[:1500]}

## {AGENT.title()} meta
- claude turns: {result.num_turns}

Generated with Claude Code (claude.com/claude-code)
""")

    pr_url = gh_pr_create(
        repo,
        title=commit_subject,
        body_file=body_file,
        head=branch,
        labels=["agent:authored", "test-coverage"],
    )
    remove_worktree(repo, wt)

    if pr_url:
        spend.increment(successes_today=1)
        msg = f"✅ {AGENT.title()} {repo}: {pr_url} (turns={result.num_turns})"
        print(msg)
        slack_post(msg)
        events.emit("pr_opened", url=pr_url, repo=f"{GH_ORG}/{repo}", turns=result.num_turns)
        events.emit("firing_complete", outcome="pr-opened")
    else:
        spend.increment(failures_today=1)
        msg = (
            f"[{AGENT.upper()}-PR-FAILED] {repo}: commit landed on {branch} but PR creation failed."
        )
        print(msg)
        slack_post(msg)
        events.emit("firing_complete", outcome="pr-create-failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
