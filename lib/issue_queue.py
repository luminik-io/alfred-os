"""Operator control over Alfred's pickup queue.

The fleet only picks up issues labeled ``agent:implement`` and never touches an
issue labeled ``do-not-pickup`` (see ``labels.py`` and ``bin/lucius.py``). This
module lets the operator flip that from the native client or Slack:

* **queue**  -> add ``agent:implement``, remove ``do-not-pickup`` and the
  ``agent:plan-pending-approval`` operator-approval gate (Alfred may pick it up).
  Queuing is the operator's go-ahead on a held Drake plan, so it releases the
  gate the same way an approved Batman bundle parent is released.
* **hold**   -> add ``do-not-pickup``, remove ``agent:implement`` (Alfred leaves it alone)
* **done**   -> close the issue (native GitHub closed state, no new label taxonomy)

``gh`` is resolved through the same augmented PATH ``shipped_board`` uses so this
works in the launchd server's bare PATH, not just the cron agents' fuller one.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from agent_runner.paths import decode_env_value, runtime_home as agent_runtime_home
from labels import DO_NOT_PICKUP, IMPLEMENT, PLAN_PENDING_APPROVAL
from shipped_board import _gh_bin, _gh_subprocess_env

logger = logging.getLogger(__name__)

_ISSUE_URL_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)")
_SLUG_NUM_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)[#\s]+(\d+)$")
_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

QUEUE_ACTIONS = ("queue", "hold", "done")
_ALLOWLIST_ENV = ("ALFRED_QUEUE_REPOS",)


def _strip_inline_comment(value: str) -> str:
    quote = ""
    escaped = False
    previous = ""
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            previous = char
            continue
        if char == "\\" and quote != "'":
            escaped = True
            previous = char
            continue
        if quote:
            if char == quote:
                quote = ""
            previous = char
            continue
        if char in ("'", '"'):
            quote = char
            previous = char
            continue
        if char == "#" and previous and previous.isspace():
            return value[:index]
        previous = char
    return value


def _runtime_config_entry(key: str) -> tuple[bool, str]:
    """Return ``(present, value)`` from process env / active runtime .env."""

    if key in os.environ:
        return True, os.environ.get(key, "").strip()
    home = _runtime_home()
    try:
        for raw_line in (home / ".env").read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line.removeprefix("export ").strip()
            name, _, raw_value = line.partition("=")
            if name.strip() == key:
                value = _strip_inline_comment(raw_value).strip()
                return True, decode_env_value(value).strip()
    except OSError:
        pass
    return False, ""


def _runtime_home() -> Path:
    return agent_runtime_home()


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
    queue_present, _ = _runtime_config_entry("ALFRED_QUEUE_REPOS")
    env_names = ("ALFRED_QUEUE_REPOS",) if queue_present else _ALLOWLIST_ENV
    repos: set[str] = set()
    for env_name in env_names:
        _, raw = _runtime_config_entry(env_name)
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

    Queuing is also the operator's go-ahead on a held Drake plan. A single-repo
    plan is filed with both ``agent:implement`` and ``agent:plan-pending-approval``
    (the operator-approval gate), so it sits unassigned until the operator
    approves it. The gate label is a pickup blocker, so simply arming
    ``agent:implement`` would not be enough: ``decide_assignment`` and Lucius both
    keep skipping the issue while the gate label is present. Queuing therefore
    removes the gate too, releasing the plan exactly the way an approved Batman
    bundle parent is released. Without this an approved single-repo plan would
    stay blocked forever.
    """
    if not _REPO_SLUG_RE.match(repo or ""):
        return False, f"invalid repo slug: {repo!r}"
    allowed = allowed_queue_repos()
    if not allowed:
        return (
            False,
            "queue repo allowlist is not configured; set ALFRED_QUEUE_REPOS",
        )
    if repo.lower() not in allowed:
        return False, f"repo not in Alfred queue allowlist: {repo}"
    if number <= 0:
        return False, f"invalid issue number: {number}"
    add_labels: tuple[str, ...]
    remove_labels: tuple[str, ...]
    if hold:
        add_labels = (DO_NOT_PICKUP,)
        remove_labels = (IMPLEMENT,)
    else:
        add_labels = (IMPLEMENT,)
        remove_labels = (DO_NOT_PICKUP, PLAN_PENDING_APPROVAL)
    base = [_gh_bin(), "issue", "edit", str(number), "-R", repo]

    def _edit(extra: list[str]) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                base + extra,
                capture_output=True,
                text=True,
                timeout=30,
                env=_gh_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError):
            logger.exception("gh issue edit failed to launch for %s#%s", repo, number)
            return False, "could not run gh issue edit"
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or "gh issue edit failed").strip()
        return True, ""

    add_args: list[str] = []
    for label in add_labels:
        add_args.extend(["--add-label", label])
    remove_args: list[str] = []
    for label in remove_labels:
        remove_args.extend(["--remove-label", label])

    # Happy path: one atomic edit toggling the adds and removes together.
    ok, err = _edit(add_args + remove_args)
    if not ok:
        # gh refuses --remove-label for a label the issue does not carry (it
        # resolves label names to IDs before the mutation), which fails the
        # whole combined edit. Don't let one missing label strand the others:
        # in a repo where agent:plan-pending-approval was never created, an
        # all-or-nothing retry on the adds alone would drop EVERY remove and
        # leave do-not-pickup in place, so the issue reports "queued" but Lucius
        # still skips it. Arm the adds first, then remove each label on its own,
        # best-effort, so do-not-pickup is always cleared.
        ok, err = _edit(add_args)
        if not ok:
            return False, err
        for label in remove_labels:
            _edit(["--remove-label", label])
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
            "queue repo allowlist is not configured; set ALFRED_QUEUE_REPOS",
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
    except (OSError, subprocess.SubprocessError):
        logger.exception("gh issue close failed to launch for %s#%s", repo, number)
        return False, "could not run gh issue close"
    if proc.returncode != 0:
        return False, (proc.stderr or "gh issue close failed").strip()
    return True, f"{repo}#{number} closed (marked done)"
