"""``gh`` CLI wrapper, label management, claim/release state machine, worktrees.

This module owns every interaction with GitHub that goes through the
``gh`` CLI plus the git-worktree primitives that bracket a firing:

* Label catalogues (:data:`STANDARD_LABELS`, :data:`LIFECYCLE_LABELS`)
  and idempotent creation via :func:`ensure_labels`.
* Issue / PR operations: :func:`gh_pr_create`, :func:`gh_issue_edit`,
  :func:`gh_issue_comment`, :func:`gh_pr_comment`,
  :func:`find_open_authored_pr_for_issue`.
* Issue claim / release state machine: :func:`claim_issue`,
  :func:`release_issue`, :func:`find_stale_claims`,
  :func:`force_release_stale_claim`, :func:`issue_dedup_check`.
* Repo-level pause overrides: :func:`is_repo_paused`,
  :func:`list_paused_repos`, :func:`set_repo_paused`.
* Per-firing git worktree lifecycle: :func:`make_worktree`,
  :func:`make_worktree_from_branch`, :func:`reuse_or_make_worktree`,
  :func:`remove_worktree`, :func:`find_existing_worktree`.

What this module does NOT own:

* Slack notification of state-machine outcomes -> ``notify.py``.
* The per-agent lock that gates entry into ``claim_issue`` ->
  ``state.py``.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import dry_run_log, is_dry_run
from .paths import (
    GH_ORG,
    PAUSED_REPOS_FILE,
    WORKSPACE,
    WORKTREE_ROOT,
    now_iso,
)
from .process import gh_json, run, short

# --------------------------------------------------------------------------
# Repo slug map + helper
# --------------------------------------------------------------------------
GH_REPO_TO_LOCAL: dict[str, str] = {}
"""Maps GitHub repo slug -> local checkout directory under WORKSPACE_ROOT.

Empty by default; consumers populate it for their fleet::

    from agent_runner import GH_REPO_TO_LOCAL
    GH_REPO_TO_LOCAL.update({
        "myorg-backend": "backend",
        "myorg-frontend": "frontend",
    })
"""

# Per-process cache for ``ensure_labels``: ``{repo_slug: True}``.
_ENSURE_LABELS_DONE: set[str] = set()


def _full_repo(slug: str) -> str:
    """Resolve a bare repo slug to ``<org>/<repo>`` using ``GH_ORG``.

    If the input already contains a ``/`` it's treated as a full slug
    and returned unchanged. If ``GH_ORG`` is unset and the input is
    bare, raise ``RuntimeError`` so the caller fails loud rather than
    calling gh with a half-formed target.
    """
    if "/" in slug:
        return slug
    if GH_ORG:
        return f"{GH_ORG}/{slug}"
    if is_dry_run():
        # Narrated lifecycle must not crash on missing GH_ORG.
        return f"dry-run-org/{slug}"
    raise RuntimeError(
        f"GH_ORG env var is unset; cannot resolve bare repo slug '{slug}' "
        "to <org>/<repo>. Set GH_ORG in your launchd plist or pass full "
        "slug like 'myorg/myrepo'."
    )


# --------------------------------------------------------------------------
# Standard label catalogues
# --------------------------------------------------------------------------
STANDARD_LABELS: list[tuple[str, str, str]] = [
    (
        "batman-pr-open",
        "5319e7",
        "A Batman bundle-PR is open in this repo. Set on PR open, cleared on merge.",
    ),
    (
        "agent:large-feature",
        "ff6b00",
        "Multi-repo feature; picked up as a bundle by Batman.",
    ),
    (
        "done-already",
        "0e8a16",
        "Issue was already implemented before Alfred picked it up.",
    ),
]
"""Default catalogue ``ensure_labels()`` creates if missing.

Consumers can ``STANDARD_LABELS.extend(...)`` to add fleet-specific
labels at import time.
"""

LIFECYCLE_LABELS: list[tuple[str, str, str]] = [
    (
        "agent:implement",
        "0e8a16",
        "Eligible for autonomous pickup by a planner agent.",
    ),
    (
        "agent:in-flight",
        "e11d21",
        "An agent is actively working this issue. Set before worktree, cleared on exit.",
    ),
    (
        "agent:pr-open",
        "fbca04",
        "A PR exists for this issue. Set by release_issue on success.",
    ),
    ("agent:done", "0e8a16", "Issue shipped. Set externally on PR merge."),
    (
        "do-not-pickup",
        "5319e7",
        "Operator override: agents must not claim this issue.",
    ),
    (
        "needs:human-scope",
        "e99695",
        "Issue requires manual scoping; not eligible for autonomous pickup.",
    ),
]
"""Framework-provided labels for the claim/release state machine."""

CLAIM_COMMENT_PREFIX = "<!-- agent-claim:"
RELEASE_COMMENT_PREFIX = "<!-- agent-release:"


# --------------------------------------------------------------------------
# Label ensure
# --------------------------------------------------------------------------


def ensure_labels(repo_slug: str, labels: list[tuple[str, str, str]] | None = None) -> None:
    """Idempotent label creation. Silent on already-exists. Cached per process."""
    if labels is None:
        labels = STANDARD_LABELS
    if is_dry_run():
        names = ", ".join(name for name, _, _ in labels)
        dry_run_log("gh", f"would ensure labels on {repo_slug}: {names}")
        return
    if repo_slug in _ENSURE_LABELS_DONE:
        return
    for name, color, desc in labels:
        run(
            [
                "gh",
                "label",
                "create",
                name,
                "--color",
                color,
                "--description",
                desc,
                "-R",
                _full_repo(repo_slug),
            ],
            timeout=15,
        )
    _ENSURE_LABELS_DONE.add(repo_slug)


# --------------------------------------------------------------------------
# Worktree lifecycle
# --------------------------------------------------------------------------


def _make_dry_run_worktree(agent: str, local_repo: str, target: str, branch: str) -> Path:
    """Build a self-contained throwaway git repo for a dry-run firing.

    The result is a real git repo in a temp dir with one commit on
    ``main`` (so an ``origin/main`` ref exists) and ``branch`` checked
    out, one synthetic commit ahead of ``main``. Falls back to a bare
    temp dir if git is unavailable.
    """
    wt = Path(tempfile.mkdtemp(prefix=f"alfred-dry-run-{agent}-{local_repo}-{target}-"))
    git_env = {
        "GIT_AUTHOR_NAME": "Alfred Dry Run",
        "GIT_AUTHOR_EMAIL": "dry-run@alfred-os.invalid",
        "GIT_COMMITTER_NAME": "Alfred Dry Run",
        "GIT_COMMITTER_EMAIL": "dry-run@alfred-os.invalid",
    }

    def _commit(message: str) -> list[str]:
        # --no-verify skips host-global pre-commit hooks; --no-gpg-sign
        # skips signing. Synthetic repo, neither is meaningful here.
        return [
            "git",
            "commit",
            "-q",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            message,
        ]

    setup_steps: list[list[str]] = [
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "commit.gpgsign", "false"],
        ["git", "config", "user.name", "Alfred Dry Run"],
        ["git", "config", "user.email", "dry-run@alfred-os.invalid"],
    ]
    for cmd in setup_steps:
        if run(cmd, cwd=str(wt), timeout=15, env=git_env).returncode != 0:
            return wt
    readme = wt / "DRY_RUN.md"
    readme.write_text(
        f"# {agent} dry-run worktree\n\n"
        f"Synthetic repo for a dry-run firing on target {target}. Not a real checkout.\n"
    )
    base_steps: list[list[str]] = [
        ["git", "add", "DRY_RUN.md"],
        _commit("chore: dry-run base commit"),
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        ["git", "checkout", "-q", "-b", branch],
    ]
    for cmd in base_steps:
        if run(cmd, cwd=str(wt), timeout=15, env=git_env).returncode != 0:
            return wt
    (wt / "dry_run_change.txt").write_text(
        f"[dry-run] synthetic change for {agent} target {target}\n"
    )
    ahead_steps: list[list[str]] = [
        ["git", "add", "dry_run_change.txt"],
        _commit(f"feat: [dry-run] synthetic implementation for {target}"),
    ]
    for cmd in ahead_steps:
        if run(cmd, cwd=str(wt), timeout=15, env=git_env).returncode != 0:
            return wt
    return wt


def make_worktree(
    local_repo: str, agent: str, target: str, base: str = "origin/main"
) -> tuple[Path, str]:
    """Create a fresh worktree on a unique branch. Returns ``(path, branch)``.

    Args:
        local_repo: directory name under ``WORKSPACE`` (or full path).
        agent: codename used in the branch + worktree directory names.
        target: short identifier (issue number, PR number, etc.).
        base: ref the new branch is created from.

    Raises:
        RuntimeError: when ``git worktree add`` fails.
    """
    repo_path = WORKSPACE / local_repo
    ts = int(time.time())
    branch = f"{agent}/{target}-{ts}"
    wt = WORKTREE_ROOT / f"eng-{agent}-{local_repo}-{target}-{ts}"

    if is_dry_run():
        wt = _make_dry_run_worktree(agent, local_repo, target, branch)
        dry_run_log(
            "git",
            f"would `git worktree add -b {branch} {wt}` from {base} in {repo_path}; "
            f"using a self-contained throwaway repo instead (no fetch, no push)",
        )
        return wt, branch

    WORKTREE_ROOT.mkdir(exist_ok=True)
    run(["git", "fetch", "origin", "main"], cwd=str(repo_path), timeout=60)
    res = run(
        ["git", "worktree", "add", "-b", branch, str(wt), base],
        cwd=str(repo_path),
        timeout=60,
    )
    if res.returncode != 0:
        raise RuntimeError(f"worktree add failed: {res.stderr.strip()}")
    return wt, branch


def make_worktree_from_branch(local_repo: str, agent: str, head_ref: str, target: str) -> Path:
    """Create a worktree pointing at an existing remote branch (read-only review)."""
    repo_path = WORKSPACE / local_repo
    ts = int(time.time())
    wt = WORKTREE_ROOT / f"eng-{agent}-{local_repo}-{target}-{ts}"

    if is_dry_run():
        wt = Path(tempfile.mkdtemp(prefix=f"alfred-dry-run-{agent}-{local_repo}-{target}-"))
        dry_run_log(
            "git",
            f"would `git worktree add {wt} origin/{head_ref}` in {repo_path}; "
            f"using throwaway temp dir instead (no fetch)",
        )
        return wt

    WORKTREE_ROOT.mkdir(exist_ok=True)
    run(["git", "fetch", "origin", head_ref], cwd=str(repo_path), timeout=60)
    res = run(
        ["git", "worktree", "add", str(wt), f"origin/{head_ref}"],
        cwd=str(repo_path),
        timeout=60,
    )
    if res.returncode != 0:
        raise RuntimeError(f"worktree add failed: {res.stderr.strip()}")
    return wt


def remove_worktree(local_repo: str, wt: Path) -> None:
    """Remove a worktree. Dry-run path cleans up the throwaway temp dir."""
    if is_dry_run():
        dry_run_log(
            "git",
            f"would `git worktree remove --force {wt}`; removing temp dir instead",
        )
        with contextlib.suppress(OSError):
            shutil.rmtree(wt, ignore_errors=True)
        return
    repo_path = WORKSPACE / local_repo
    run(
        ["git", "worktree", "remove", "--force", str(wt)],
        cwd=str(repo_path),
        timeout=30,
    )


def find_existing_worktree(local_repo: str, agent: str, target: str) -> Path | None:
    """Locate a previous-firing worktree for ``(agent, local_repo, target)``.

    Returns the most recent matching path under ``WORKTREE_ROOT`` or
    ``None`` if no leftover worktree exists.
    """
    if not WORKTREE_ROOT.exists():
        return None
    pattern = f"eng-{agent}-{local_repo}-{target}-*"
    matches = sorted(
        (p for p in WORKTREE_ROOT.glob(pattern) if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def _worktree_branch(wt: Path) -> str | None:
    """Return the branch checked out inside ``wt`` or ``None`` on error."""
    res = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(wt), timeout=10)
    if res.returncode != 0:
        return None
    branch = (res.stdout or "").strip()
    return branch or None


def _worktree_is_stale(local_repo: str, wt: Path, base: str = "origin/main") -> bool:
    """True when ``wt``'s branch is detached, or base has moved past with no commits ahead."""
    repo_path = WORKSPACE / local_repo
    run(["git", "fetch", "origin", "main"], cwd=str(repo_path), timeout=60)
    branch = _worktree_branch(wt)
    if not branch or branch == "HEAD":
        return True
    behind_ahead = run(
        ["git", "rev-list", "--left-right", "--count", f"{base}...{branch}"],
        cwd=str(wt),
        timeout=10,
    )
    if behind_ahead.returncode != 0:
        return True
    parts = (behind_ahead.stdout or "").strip().split()
    if len(parts) != 2:
        return True
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return True
    return ahead == 0 and behind > 0


def reuse_or_make_worktree(
    local_repo: str, agent: str, target: str, *, base: str = "origin/main"
) -> tuple[Path, str, bool]:
    """Reuse a previous-firing worktree when one exists; else fall back to fresh.

    Returns ``(path, branch, reused)`` where ``reused`` is ``True``
    when we landed on a leftover worktree from a prior firing.
    """
    existing = find_existing_worktree(local_repo, agent, target)
    if existing is None:
        wt, branch = make_worktree(local_repo, agent, target, base=base)
        return wt, branch, False
    if _worktree_is_stale(local_repo, existing, base=base):
        with contextlib.suppress(Exception):
            remove_worktree(local_repo, existing)
        wt, branch = make_worktree(local_repo, agent, target, base=base)
        return wt, branch, False
    branch = _worktree_branch(existing) or ""
    if not branch:
        with contextlib.suppress(Exception):
            remove_worktree(local_repo, existing)
        wt, branch = make_worktree(local_repo, agent, target, base=base)
        return wt, branch, False
    # Reuse: refresh local view of main inside the worktree so the resumed
    # firing sees the latest base; the caller decides whether to rebase.
    run(["git", "fetch", "origin", "main"], cwd=str(existing), timeout=60)
    return existing, branch, True


# --------------------------------------------------------------------------
# gh CLI PR / issue / comment helpers
# --------------------------------------------------------------------------


def gh_pr_create(
    repo_slug: str,
    *,
    title: str,
    body_file: Path,
    head: str | None = None,
    labels: list[str] | None = None,
    base: str = "main",
    draft: bool = False,
) -> str | None:
    """Open a PR. Pre-ensures labels exist. Returns PR URL or ``None`` on failure.

    Also opportunistically creates any ad-hoc labels not in
    :data:`STANDARD_LABELS` with a neutral grey colour. Logs the gh
    stderr on failure so the firing's stderr / Slack alert path carries
    a real cause string instead of an opaque ``None``.
    """
    if is_dry_run():
        fake_url = f"https://github.com/{_full_repo(repo_slug)}/pull/0"
        label_part = f", labels={labels}" if labels else ""
        dry_run_log(
            "gh",
            f"would `gh pr create` on {_full_repo(repo_slug)}: "
            f"title={title!r}, head={head or '(default)'}, base={base}, "
            f"draft={draft}{label_part} -> {fake_url}",
        )
        return fake_url

    if labels:
        ensure_labels(repo_slug)
        standard_names = {name for name, _, _ in STANDARD_LABELS}
        adhoc = [lbl for lbl in labels if lbl not in standard_names]
        for lbl in adhoc:
            run(
                [
                    "gh",
                    "label",
                    "create",
                    lbl,
                    "--color",
                    "ededed",
                    "--description",
                    "Auto-created by gh_pr_create on first use",
                    "-R",
                    _full_repo(repo_slug),
                ],
                timeout=15,
            )
    cmd = [
        "gh",
        "pr",
        "create",
        "-R",
        _full_repo(repo_slug),
        "--title",
        title,
        "--body-file",
        str(body_file),
        "--base",
        base,
    ]
    if head:
        cmd.extend(["--head", head])
    if draft:
        cmd.append("--draft")
    for label in labels or []:
        cmd.extend(["--label", label])
    res = run(cmd, timeout=60)
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        stdout = (res.stdout or "").strip()
        print(
            f"[gh_pr_create] FAILED repo={_full_repo(repo_slug)} "
            f"head={head or '(default)'} base={base} "
            f"title={title[:80]!r} rc={res.returncode}\n"
            f"  stderr: {stderr[:600]}\n"
            f"  stdout: {stdout[:200]}",
            file=sys.stderr,
        )
        return None
    for line in reversed((res.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("https://"):
            return line
    return None


def gh_issue_edit(
    repo_slug: str,
    num: int,
    *,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> bool:
    """Add or remove labels on an issue. Returns ``True`` on gh-success."""
    if is_dry_run():
        dry_run_log(
            "gh",
            f"would `gh issue edit #{num}` on {_full_repo(repo_slug)}: "
            f"add={add_labels or []}, remove={remove_labels or []}",
        )
        return True
    if add_labels:
        ensure_labels(repo_slug)
    cmd = ["gh", "issue", "edit", str(num), "-R", _full_repo(repo_slug)]
    for label in add_labels or []:
        cmd.extend(["--add-label", label])
    for label in remove_labels or []:
        cmd.extend(["--remove-label", label])
    res = run(cmd, timeout=30)
    return res.returncode == 0


def gh_issue_comment(repo_slug: str, num: int, body: str) -> bool:
    """Post a comment on an issue. Returns ``True`` on gh-success."""
    if is_dry_run():
        dry_run_log(
            "gh",
            f"would `gh issue comment #{num}` on {_full_repo(repo_slug)}: {short(body, 200)}",
        )
        return True
    res = run(
        [
            "gh",
            "issue",
            "comment",
            str(num),
            "-R",
            _full_repo(repo_slug),
            "--body",
            body,
        ],
        timeout=30,
    )
    return res.returncode == 0


def gh_pr_comment(repo_slug: str, num: int, body: str) -> bool:
    """Post a comment on a PR. Returns ``True`` on gh-success."""
    if is_dry_run():
        dry_run_log(
            "gh",
            f"would `gh pr comment #{num}` on {_full_repo(repo_slug)}: {short(body, 200)}",
        )
        return True
    res = run(
        [
            "gh",
            "pr",
            "comment",
            str(num),
            "-R",
            _full_repo(repo_slug),
            "--body",
            body,
        ],
        timeout=30,
    )
    return res.returncode == 0


def find_open_authored_pr_for_issue(
    repo_slug: str, issue_num: int, *, label: str = "agent:authored"
) -> dict | None:
    """Return the first open agent-authored PR that references ``issue_num``.

    Searches any open PR whose title or body mentions ``#<issue_num>``
    and carries ``label`` (so a third-party human PR that happens to
    reference the issue does NOT lock the queue). Returns the PR JSON
    dict or ``None`` if no such PR exists.
    """
    prs = gh_json(
        [
            "gh",
            "pr",
            "list",
            "-R",
            _full_repo(repo_slug),
            "--state",
            "open",
            "--search",
            f'"#{issue_num}" in:title,body',
            "--json",
            "number,url,state,labels,title,body",
            "--limit",
            "10",
        ],
        default=[],
    )
    for pr in prs or []:
        pr_labels = {label_obj.get("name") for label_obj in pr.get("labels", [])}
        if label and label not in pr_labels:
            continue
        # gh's text search substring-matches, so a PR mentioning #12345
        # matches a search for #12. Re-validate the body+title contain
        # the exact issue token followed by a non-digit so we never lock
        # issue #12 behind a PR that closes #1234.
        token = f"#{issue_num}"
        haystack = f" {pr.get('title', '')} {pr.get('body', '') or ''} "
        idx = haystack.find(token)
        valid = False
        while idx >= 0:
            after = haystack[idx + len(token) : idx + len(token) + 1]
            if not after.isdigit():
                valid = True
                break
            idx = haystack.find(token, idx + 1)
        if not valid:
            continue
        return pr
    return None


# --------------------------------------------------------------------------
# Paused repos
# --------------------------------------------------------------------------


def is_repo_paused(repo_slug: str) -> bool:
    """Is this repo currently paused (operator override)?

    Reads ``${ALFRED_HOME}/state/paused-repos.json``. Missing or
    unparseable -> ``False`` (fail-open).
    """
    if not PAUSED_REPOS_FILE.exists():
        return False
    try:
        data = json.loads(PAUSED_REPOS_FILE.read_text())
    except (json.JSONDecodeError, ValueError, OSError):
        return False
    return repo_slug in (data.get("paused", []) or [])


def list_paused_repos() -> list[str]:
    """Return the list of currently-paused repo slugs."""
    if not PAUSED_REPOS_FILE.exists():
        return []
    try:
        return list(json.loads(PAUSED_REPOS_FILE.read_text()).get("paused", []) or [])
    except (json.JSONDecodeError, ValueError, OSError):
        return []


def set_repo_paused(repo_slug: str, paused: bool) -> list[str]:
    """Add or remove a repo from the paused list. Returns the new full list."""
    PAUSED_REPOS_FILE.parent.mkdir(parents=True, exist_ok=True)
    current = set(list_paused_repos())
    if paused:
        current.add(repo_slug)
    else:
        current.discard(repo_slug)
    out = sorted(current)
    PAUSED_REPOS_FILE.write_text(json.dumps({"paused": out}, indent=2))
    return out


# --------------------------------------------------------------------------
# Claim / release state machine
# --------------------------------------------------------------------------


def _parse_claim_comment(body: str) -> dict:
    """Parse ``codename=X firing_id=Y outcome=Z ts=W`` from a claim/release comment body."""
    out: dict = {}
    payload = body.strip()
    for prefix in (CLAIM_COMMENT_PREFIX, RELEASE_COMMENT_PREFIX):
        if payload.startswith(prefix):
            payload = payload[len(prefix) :]
            break
    if payload.endswith("-->"):
        payload = payload[:-3]
    for part in payload.split():
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


def _issue_state(repo_slug: str, num: int) -> dict:
    """One-shot fetch of labels + comments + state for claim/release/sweep."""
    return gh_json(
        [
            "gh",
            "issue",
            "view",
            str(num),
            "-R",
            _full_repo(repo_slug),
            "--json",
            "labels,state,comments,number",
        ],
        default={"labels": [], "state": "OPEN", "comments": [], "number": num},
    )


def claim_issue(repo_slug: str, num: int, *, codename: str, firing_id: str) -> bool:
    """Atomic-ish claim. Returns ``True`` if the claim succeeded, ``False`` if blocked.

    Refusal reasons (returns ``False``):

    * the repo is paused via :func:`set_repo_paused`;
    * the issue is closed;
    * the issue carries any of ``agent:in-flight``, ``agent:pr-open``,
      ``do-not-pickup``, ``needs:human-scope``;
    * race: another claim comment with an earlier ``createdAt`` exists
      with no matching release comment (we back out cleanly).

    Side effects on success: remove ``agent:implement``, add
    ``agent:in-flight``, post a structured claim comment for the audit
    trail.
    """
    if is_dry_run():
        dry_run_log(
            "gh",
            f"would claim {_full_repo(repo_slug)}#{num} for {codename} "
            f"(firing_id={firing_id}): add agent:in-flight, post claim comment",
        )
        return True
    if is_repo_paused(repo_slug):
        return False
    state = _issue_state(repo_slug, num)
    if state.get("state") != "OPEN":
        return False
    labels = {lbl["name"] for lbl in state.get("labels", [])}
    blockers = labels & {
        "agent:in-flight",
        "agent:pr-open",
        "do-not-pickup",
        "needs:human-scope",
    }
    if blockers:
        return False
    ensure_labels(repo_slug, LIFECYCLE_LABELS)
    if not gh_issue_edit(
        repo_slug,
        num,
        add_labels=["agent:in-flight"],
        remove_labels=["agent:implement"],
    ):
        return False
    claim_body = (
        f"{CLAIM_COMMENT_PREFIX}codename={codename} firing_id={firing_id} ts={now_iso()} -->"
    )
    if not gh_issue_comment(repo_slug, num, claim_body):
        gh_issue_edit(
            repo_slug,
            num,
            add_labels=["agent:implement"],
            remove_labels=["agent:in-flight"],
        )
        return False
    contested_by = _detect_contested_claim(repo_slug, num, codename=codename, firing_id=firing_id)
    if contested_by is not None:
        gh_issue_edit(
            repo_slug,
            num,
            add_labels=["agent:implement"],
            remove_labels=["agent:in-flight"],
        )
        gh_issue_comment(
            repo_slug,
            num,
            f"{RELEASE_COMMENT_PREFIX}codename={codename} firing_id={firing_id} "
            f"outcome=race-yielded-to={contested_by} ts={now_iso()} -->",
        )
        return False
    return True


def release_issue(
    repo_slug: str,
    num: int,
    *,
    codename: str,
    firing_id: str,
    outcome: str = "success",
    transition_to: str | None = None,
    pr_url: str | None = None,
) -> bool:
    """Release a claim, optionally transitioning to a follow-up label.

    Args:
        repo_slug: e.g. ``"myorg/backend"`` or bare ``"backend"``.
        num: issue number.
        codename: emitting agent.
        firing_id: firing identifier for the audit trail.
        outcome: free-form string recorded in the release comment.
            Conventional values: ``success``, ``failure``, ``partial``,
            ``no-commit``, ``rate-limit``, ``max-turns``,
            ``already-implemented``, ``race-yielded``, ``stale-swept``.
        transition_to: optional successor label such as ``agent:pr-open``
            or ``agent:done``. ``None`` returns the issue to the
            ``agent:implement`` queue so it can be re-picked.
        pr_url: optional URL recorded in the release comment.

    Returns:
        ``True`` on confirmed label edit + comment post.
    """
    if is_dry_run():
        target = transition_to or "agent:implement"
        pr_part = f", pr={pr_url}" if pr_url else ""
        dry_run_log(
            "gh",
            f"would release {_full_repo(repo_slug)}#{num} for {codename} "
            f"(firing_id={firing_id}): outcome={outcome}{pr_part}, "
            f"remove agent:in-flight, add {target}",
        )
        return True
    add: list[str] = []
    remove = ["agent:in-flight"]
    if transition_to:
        add.append(transition_to)
    else:
        add.append("agent:implement")
    edited = gh_issue_edit(repo_slug, num, add_labels=add, remove_labels=remove)
    pr_part = f" pr={pr_url}" if pr_url else ""
    commented = gh_issue_comment(
        repo_slug,
        num,
        f"{RELEASE_COMMENT_PREFIX}codename={codename} firing_id={firing_id} "
        f"outcome={outcome}{pr_part} ts={now_iso()} -->",
    )
    return edited and commented


def _detect_contested_claim(
    repo_slug: str, num: int, *, codename: str, firing_id: str
) -> str | None:
    """Return ``"codename:firing_id"`` of the contesting claimant on race-loss, else ``None``."""
    state = _issue_state(repo_slug, num)
    comments = state.get("comments", [])
    claims: dict[tuple, str] = {}
    releases: set[tuple] = set()
    for c in comments[-50:]:
        body = (c.get("body") or "").strip()
        created = c.get("createdAt") or ""
        if body.startswith(CLAIM_COMMENT_PREFIX):
            meta = _parse_claim_comment(body)
            key = (meta.get("codename"), meta.get("firing_id"))
            if key not in claims:
                claims[key] = created
        elif body.startswith(RELEASE_COMMENT_PREFIX):
            meta = _parse_claim_comment(body)
            releases.add((meta.get("codename"), meta.get("firing_id")))
    own_key = (codename, firing_id)
    own_ts = claims.get(own_key, "")
    if not own_ts:
        return None
    for key, ts in claims.items():
        if key == own_key or key in releases:
            continue
        if ts and ts < own_ts:
            return f"{key[0]}:{key[1]}"
    return None


def find_stale_claims(repo_slug: str, *, max_age_hours: int = 4) -> list[dict]:
    """List in-flight issues whose latest unreleased claim is older than ``max_age_hours``.

    Returns dicts with ``number`` / ``title`` / ``codename`` /
    ``firing_id`` / ``age_hours``. The caller decides whether to
    force-release.
    """
    issues = gh_json(
        [
            "gh",
            "issue",
            "list",
            "-R",
            _full_repo(repo_slug),
            "--label",
            "agent:in-flight",
            "--state",
            "open",
            "--json",
            "number,title",
            "--limit",
            "100",
        ],
        default=[],
    )
    cutoff = datetime.now(UTC).timestamp() - max_age_hours * 3600
    stale: list[dict] = []
    for issue in issues:
        num = issue["number"]
        state = _issue_state(repo_slug, num)
        comments = state.get("comments", [])
        latest_claim_ts: str | None = None
        latest_claim_meta: dict | None = None
        releases: set[tuple] = set()
        for c in comments:
            body = (c.get("body") or "").strip()
            if body.startswith(CLAIM_COMMENT_PREFIX):
                meta = _parse_claim_comment(body)
                latest_claim_ts = c.get("createdAt") or latest_claim_ts
                latest_claim_meta = meta
            elif body.startswith(RELEASE_COMMENT_PREFIX):
                meta = _parse_claim_comment(body)
                releases.add((meta.get("codename"), meta.get("firing_id")))
        if not latest_claim_meta or not latest_claim_ts:
            stale.append(
                {
                    "repo": repo_slug,
                    "number": num,
                    "title": issue.get("title", ""),
                    "codename": "?",
                    "firing_id": "?",
                    "age_hours": float("inf"),
                }
            )
            continue
        key = (
            latest_claim_meta.get("codename"),
            latest_claim_meta.get("firing_id"),
        )
        if key in releases:
            stale.append(
                {
                    "repo": repo_slug,
                    "number": num,
                    "title": issue.get("title", ""),
                    "codename": key[0] or "?",
                    "firing_id": key[1] or "?",
                    "age_hours": 0.0,
                    "label_drift": True,
                }
            )
            continue
        try:
            ts = datetime.strptime(
                latest_claim_ts.replace("Z", "+0000"),
                "%Y-%m-%dT%H:%M:%S%z",
            ).timestamp()
        except (ValueError, AttributeError):
            continue
        if ts < cutoff:
            stale.append(
                {
                    "repo": repo_slug,
                    "number": num,
                    "title": issue.get("title", ""),
                    "codename": key[0] or "?",
                    "firing_id": key[1] or "?",
                    "age_hours": (datetime.now(UTC).timestamp() - ts) / 3600,
                }
            )
    return stale


def force_release_stale_claim(
    repo_slug: str,
    num: int,
    *,
    sweep_id: str,
    released_codename: str | None = None,
    released_firing_id: str | None = None,
) -> bool:
    """Forcibly release a stale claim and restore ``agent:implement``.

    The release comment is written under the stale claim's original
    ``(codename, firing_id)`` so future claim detection can pair the
    release with the original claim. ``sweep_id`` remains in metadata
    for audit.
    """
    if is_dry_run():
        codename = released_codename or "cleanup"
        firing_id = released_firing_id or sweep_id
        dry_run_log(
            "gh",
            f"would force-release stale claim {_full_repo(repo_slug)}#{num} "
            f"(original {codename}/{firing_id}, swept_by={sweep_id}): "
            f"remove agent:in-flight, add agent:implement",
        )
        return True
    edited = gh_issue_edit(
        repo_slug,
        num,
        add_labels=["agent:implement"],
        remove_labels=["agent:in-flight"],
    )
    codename = released_codename or "cleanup"
    firing_id = released_firing_id or sweep_id
    commented = gh_issue_comment(
        repo_slug,
        num,
        f"{RELEASE_COMMENT_PREFIX}codename={codename} firing_id={firing_id} "
        f"outcome=stale-swept swept_by={sweep_id} ts={now_iso()} -->",
    )
    return edited and commented


def issue_dedup_check(repo_slug: str, num: int) -> dict:
    """Return a structured dedup status for an issue.

    Used by operator CLI helpers and pre-push hooks to decide whether
    claiming or pushing an issue-referencing branch would race an
    in-flight agent.
    """
    state = _issue_state(repo_slug, num)
    labels = [lbl["name"] for lbl in state.get("labels", [])]
    comments = state.get("comments", [])
    latest_claim: dict | None = None
    for c in reversed(comments[-50:]):
        body = (c.get("body") or "").strip()
        if body.startswith(CLAIM_COMMENT_PREFIX):
            latest_claim = _parse_claim_comment(body)
            latest_claim["createdAt"] = c.get("createdAt", "")
            break
    return {
        "repo": repo_slug,
        "number": num,
        "state": state.get("state"),
        "labels": labels,
        "in_flight": "agent:in-flight" in labels,
        "pr_open": "agent:pr-open" in labels,
        "do_not_pickup": "do-not-pickup" in labels,
        "needs_human_scope": "needs:human-scope" in labels,
        "claimable": (
            state.get("state") == "OPEN"
            and "agent:in-flight" not in labels
            and "agent:pr-open" not in labels
            and "do-not-pickup" not in labels
            and "needs:human-scope" not in labels
            and not is_repo_paused(repo_slug)
        ),
        "latest_claim": latest_claim,
        "repo_paused": is_repo_paused(repo_slug),
    }
