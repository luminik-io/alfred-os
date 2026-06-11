"""Operator control over Alfred's pickup queue.

The fleet only picks up issues labeled ``agent:implement`` and never touches an
issue labeled ``do-not-pickup`` (see ``labels.py`` and ``bin/lucius.py``). This
module lets the operator flip that from the native client or Slack:

* **queue**  -> add ``agent:implement``, remove ``do-not-pickup`` (Alfred may pick it up)
* **hold**   -> add ``do-not-pickup``, remove ``agent:implement`` (Alfred leaves it alone)
* **done**   -> close the issue (native GitHub closed state, no new label taxonomy)

``gh`` is resolved through the same augmented PATH ``shipped_board`` uses so this
works in the launchd server's bare PATH, not just the cron agents' fuller one.
"""

from __future__ import annotations

import re
import subprocess

from labels import DO_NOT_PICKUP, IMPLEMENT
from shipped_board import _config_value, _gh_bin, _gh_subprocess_env

_ISSUE_URL_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)")
_SLUG_NUM_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)[#\s]+(\d+)$")
_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

QUEUE_ACTIONS = ("queue", "hold", "done")
_ALLOWLIST_ENV = ("ALFRED_QUEUE_REPOS", "ALFRED_SHIPPED_REPOS", "ALFRED_BRIDGE_REPOS")


def parse_issue_ref(text: str) -> tuple[str, int] | None:
    """Parse an issue reference into ``(owner/repo, number)`` or ``None``.

    Accepts a GitHub issue URL (``https://github.com/org/repo/issues/12``, with
    or without scheme / ``<>`` Slack wrapping) or an explicit ``org/repo#12`` /
    ``org/repo 12``. A bare number is rejected: it has no repo, so it would be
    ambiguous and unsafe to act on.
    """
    if not text:
        return None
    cleaned = text.strip().strip("<>").strip()
    url = _ISSUE_URL_RE.search(cleaned)
    if url:
        return url.group(1), int(url.group(2))
    slug = _SLUG_NUM_RE.match(cleaned)
    if slug:
        return slug.group(1), int(slug.group(2))
    return None


def allowed_queue_repos() -> set[str]:
    """Configured repos queue/hold is allowed to mutate.

    Queue control changes issue labels that directly affect autonomous pickup,
    so it must be scoped at least as tightly as the Slack issue bridge. Require
    an explicit allowlist and never infer from arbitrary GitHub access.
    """
    repos: set[str] = set()
    for env_name in _ALLOWLIST_ENV:
        raw = _config_value(env_name)
        for item in re.split(r"[\s,]+", raw):
            repo = item.strip().lower()
            if repo:
                repos.add(repo)
    return repos


def set_issue_pickup(repo: str, number: int, *, hold: bool) -> tuple[bool, str]:
    """Add or remove the pickup label on an issue. Returns ``(ok, detail)``.

    ``hold=True`` takes the issue out of Alfred's reach (``do-not-pickup``);
    ``hold=False`` makes it eligible (``agent:implement``). Mutually exclusive
    labels are toggled together so the issue never carries both.
    """
    if not _REPO_SLUG_RE.match(repo or ""):
        return False, f"invalid repo slug: {repo!r}"
    allowed = allowed_queue_repos()
    if not allowed:
        return (
            False,
            "queue repo allowlist is not configured; set ALFRED_QUEUE_REPOS, "
            "ALFRED_SHIPPED_REPOS, or ALFRED_BRIDGE_REPOS",
        )
    if repo.lower() not in allowed:
        return False, f"repo not in Alfred queue allowlist: {repo}"
    if number <= 0:
        return False, f"invalid issue number: {number}"
    add, remove = (DO_NOT_PICKUP, IMPLEMENT) if hold else (IMPLEMENT, DO_NOT_PICKUP)
    cmd = [
        _gh_bin(),
        "issue",
        "edit",
        str(number),
        "-R",
        repo,
        "--add-label",
        add,
        "--remove-label",
        remove,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env=_gh_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        # gh refuses --remove-label for a label the issue doesn't have; retry
        # with only the add so a fresh issue can still be queued/held.
        retry = [
            _gh_bin(),
            "issue",
            "edit",
            str(number),
            "-R",
            repo,
            "--add-label",
            add,
        ]
        try:
            proc2 = subprocess.run(
                retry,
                capture_output=True,
                text=True,
                timeout=30,
                env=_gh_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if proc2.returncode != 0:
            return False, (proc2.stderr or proc.stderr or "gh issue edit failed").strip()
    verb = "held (Alfred will not pick it up)" if hold else "queued for Alfred"
    return True, f"{repo}#{number} {verb}"


def close_issue(repo: str, number: int) -> tuple[bool, str]:
    """Close an issue on GitHub. Returns ``(ok, detail)``.

    This marks the work done using GitHub's native closed state, the same
    place ``gh issue close`` and the web UI write to. There is no new label
    taxonomy: the issue simply stops being open. The guards mirror
    ``set_issue_pickup`` (valid repo slug, scoped allowlist, positive issue
    number) so an operator can never close an issue outside Alfred's configured
    repos. ``gh`` is already idempotent on an already-closed issue, so a repeat
    Done is harmless.
    """
    if not _REPO_SLUG_RE.match(repo or ""):
        return False, f"invalid repo slug: {repo!r}"
    allowed = allowed_queue_repos()
    if not allowed:
        return (
            False,
            "queue repo allowlist is not configured; set ALFRED_QUEUE_REPOS, "
            "ALFRED_SHIPPED_REPOS, or ALFRED_BRIDGE_REPOS",
        )
    if repo.lower() not in allowed:
        return False, f"repo not in Alfred queue allowlist: {repo}"
    if number <= 0:
        return False, f"invalid issue number: {number}"
    cmd = [
        _gh_bin(),
        "issue",
        "close",
        str(number),
        "-R",
        repo,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env=_gh_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or "gh issue close failed").strip()
    return True, f"{repo}#{number} closed (marked done)"
