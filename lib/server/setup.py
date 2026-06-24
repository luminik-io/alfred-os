"""First-run setup + onboarding helpers for ``alfred serve``.

These back the client-owned **Set up** surface so a non-developer can get from
zero to a working fleet without a terminal:

* :func:`bootstrap_status`  - what is connected vs missing (gh auth, engine
  CLIs, watched repos, runtime). One read the client turns into a clear
  next-action per row.
* :func:`list_owner_repos`  - the operator's own GitHub repos via
  ``gh repo list`` plus the repos already selected, so the client can render a
  checklist with the current selection ticked.
* :func:`persist_selected_repos`  - write the chosen repo allowlist to
  ``$ALFRED_HOME/.env`` (the same keys ``shipped_board`` / ``issue_queue``
  read), so the choice survives a restart and scopes everything Alfred touches.
* :func:`STARTER_PLAYBOOKS`  - 2-3 canned overnight jobs the client can compose
  into a concrete first request.
* the demo store (:func:`seed_demo`, :func:`clear_demo`, :func:`load_demo_cards`)
  - a few clearly-labelled sample board cards persisted locally (never on
  GitHub) so the empty board teaches what Alfred looks like in use.

All ``gh`` access goes through the same augmented-PATH resolver
:mod:`shipped_board` uses, so this works under the bare-PATH launchd server.
No repo names are hardcoded, so the behaviour is identical in the public OSS
twin.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_runner.paths import config_value
from issue_queue import allowed_queue_repos
from shipped_board import _gh_bin, _gh_subprocess_env

# The watched-repo allowlist the rest of the fleet reads. The Set up surface
# writes BOTH the queue allowlist (controls what an operator can arm/hold/close)
# and the shipped allowlist (controls which repos the board scans), so the one
# golden-path repo pick wires up the whole experience, including the native
# Plan-work -> GitHub issue handoff and the Slack issue bridge.
QUEUE_REPOS_ENV = "ALFRED_QUEUE_REPOS"
SHIPPED_REPOS_ENV = "ALFRED_SHIPPED_REPOS"
BRIDGE_REPOS_ENV = "ALFRED_BRIDGE_REPOS"
_REPO_ENV_KEYS = (QUEUE_REPOS_ENV, SHIPPED_REPOS_ENV, BRIDGE_REPOS_ENV)

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Engine CLIs Alfred rides. Detected by presence on PATH only (no version
# spawn): the golden path needs at least one of these signed-in subscription
# CLIs, never an API key paste.
_ENGINE_BINS = ("claude", "codex")

_DEMO_FILENAME = "setup-demo-cards.json"
# A made-up slug the demo cards live under. It is never a real ``owner/repo``,
# so a demo card can never be mistaken for (or acted on as) real fleet work.
DEMO_REPO = "alfred/demo"


# --------------------------------------------------------------------------- #
# Repo slug validation
# --------------------------------------------------------------------------- #
def normalize_repo_slugs(values: Any) -> list[str]:
    """De-dup + validate a list of ``owner/repo`` slugs, dropping junk.

    Order-preserving, case-folded to lower (GitHub slugs are case-insensitive
    and the queue allowlist compares lower-cased). A value that is not a valid
    ``owner/repo`` slug is dropped rather than raising, so a partly-bad payload
    still persists the good repos. Returns ``[]`` for any non-list input.
    """
    if not isinstance(values, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        slug = str(raw or "").strip().lower()
        if not _REPO_SLUG_RE.match(slug):
            continue
        # ``..`` is a valid token under the slug char class but a path-traversal
        # hazard for any consumer that resolves a slug to a workspace dir, so a
        # ``..`` owner or repo segment is dropped at this chokepoint.
        if any(part == ".." for part in slug.split("/")):
            continue
        if slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
    return out


def selected_repos() -> list[str]:
    """The repos currently scoped to Alfred, from the config the fleet reads.

    Reads the queue allowlist (``allowed_queue_repos``) so the Set up surface
    shows the same scope queue/hold/close actually enforce. Sorted for a stable
    render.
    """
    return sorted(allowed_queue_repos())


# --------------------------------------------------------------------------- #
# .env writer
# --------------------------------------------------------------------------- #
def _env_path() -> Path:
    home = os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")
    return Path(home) / ".env"


def _format_repo_value(repos: list[str]) -> str:
    return ",".join(repos)


def write_env_values(values: dict[str, str]) -> Path:
    """Upsert environment variable lines into ``$ALFRED_HOME/.env``, preserving the rest.

    This is the one place the Set up surface persists config. An existing line
    for a managed key is replaced in place (so comments and ordering around it
    survive); a missing key is appended. The file is written atomically via a
    temp file + replace with ``0600`` perms so a reader never sees a half-written
    file and the secrets-bearing env file is never world-readable.

    Only keys matching ``_ENV_KEY_RE`` are accepted, so a caller can never smuggle
    a newline or a comment-injection into the file.
    """
    for key in values:
        if not _ENV_KEY_RE.match(key):
            raise ValueError(f"unsafe env key: {key!r}")
    for value in values.values():
        if "\n" in value or "\r" in value:
            raise ValueError("env values may not contain newlines")

    path = _env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        existing = []

    remaining = dict(values)
    out_lines: list[str] = []
    for line in existing:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name = stripped.partition("=")[0].strip()
            if name in remaining:
                out_lines.append(f"{name}={remaining.pop(name)}")
                continue
        out_lines.append(line)
    for name, value in remaining.items():
        out_lines.append(f"{name}={value}")

    body = "\n".join(out_lines).rstrip("\n") + "\n"
    tmp = path.with_name(f"{path.name}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    with suppress(OSError):
        os.chmod(path, 0o600)
    return path


def persist_selected_repos(repos: list[str]) -> dict[str, Any]:
    """Persist the chosen repo allowlist and mirror it into the live process.

    Writes the queue + shipped allowlist keys to ``.env`` AND updates
    ``os.environ`` so the change takes effect for this running server without a
    restart (``config_value`` prefers the process env, and a fresh board /
    queue call then sees the new scope immediately). Returns the persisted
    config so the client can show it back transparently.
    """
    clean = normalize_repo_slugs(repos)
    value = _format_repo_value(clean)
    env_path = write_env_values(dict.fromkeys(_REPO_ENV_KEYS, value))
    for key in _REPO_ENV_KEYS:
        # Mirror into the live process so the new scope is effective now. An
        # empty selection clears the override so the resolver falls back to the
        # .env value (also empty), which is the honest "nothing scoped" state.
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    return {
        "repos": clean,
        "env_path": str(env_path),
        "keys": list(_REPO_ENV_KEYS),
    }


# --------------------------------------------------------------------------- #
# gh + engine detection
# --------------------------------------------------------------------------- #
def gh_auth_status() -> dict[str, Any]:
    """Probe ``gh auth status`` and report a plain-language verdict.

    Returns ``{ok, account, detail}``. ``ok`` is True when ``gh`` is installed
    and reports an authenticated account. Never raises: a missing binary or a
    failed probe degrades to ``ok=False`` with a human ``detail`` so the client
    shows a clear next action ("run gh auth login") instead of an error.
    """
    gh = _gh_bin()
    if shutil.which(gh) is None and not os.path.isabs(gh):
        return {
            "ok": False,
            "account": None,
            "detail": "GitHub CLI (gh) is not installed.",
        }
    try:
        proc = subprocess.run(
            [gh, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=15,
            env=_gh_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "account": None,
            "detail": f"Could not run gh auth status: {type(exc).__name__}.",
        }
    # gh writes the human status to stderr; merge both so the account parse and
    # the surfaced detail see the same text regardless of gh version.
    text = f"{proc.stdout}\n{proc.stderr}".strip()
    if proc.returncode != 0:
        return {
            "ok": False,
            "account": None,
            "detail": "Not signed in to GitHub. Run gh auth login once.",
        }
    account = _parse_gh_account(text)
    return {
        "ok": True,
        "account": account,
        "detail": (f"Signed in to GitHub as {account}." if account else "Signed in to GitHub."),
    }


def _parse_gh_account(text: str) -> str | None:
    match = re.search(r"account\s+([A-Za-z0-9-]+)", text)
    if match:
        return match.group(1)
    match = re.search(r"Logged in to [^ ]+ as ([A-Za-z0-9-]+)", text)
    if match:
        return match.group(1)
    return None


def engine_clis() -> list[dict[str, Any]]:
    """Detect the engine CLIs Alfred rides (claude / codex) on PATH.

    Server-side detection: presence-only via the augmented search path the gh
    resolver uses, so a launchd-bare-PATH server still finds Homebrew installs.
    The native client may also probe deeper (``alfred auth status``); this is
    the in-browser-capable fallback so the runtime checks work without Tauri.
    Honours ``CLAUDE_BIN`` / ``CODEX_BIN`` overrides via config.
    """
    search = os.pathsep.join((*_engine_search_path(), os.environ.get("PATH", "")))
    out: list[dict[str, Any]] = []
    for name in _ENGINE_BINS:
        configured = config_value(f"{name.upper()}_BIN")
        resolved = (
            configured
            if configured and (os.path.isabs(configured) or shutil.which(configured, path=search))
            else shutil.which(name, path=search)
        )
        out.append(
            {
                "name": name,
                "installed": bool(resolved),
                "path": resolved,
            }
        )
    return out


def _engine_search_path() -> tuple[str, ...]:
    return (
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/.claude/local"),
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
    )


def bootstrap_status() -> dict[str, Any]:
    """One read the client turns into the Set up checklist.

    Surfaces what is connected vs missing with a next action per row:
    GitHub auth, at least one engine CLI, the watched-repo selection, and a
    demo-present flag. ``ready`` is the golden-path gate: gh authed + at least
    one engine + at least one repo selected (no AWS / Slack required).
    """
    gh = gh_auth_status()
    engines = engine_clis()
    repos = selected_repos()
    any_engine = any(e["installed"] for e in engines)
    return {
        "github": gh,
        "engines": engines,
        "engine_ready": any_engine,
        "repos": {
            "selected": repos,
            "count": len(repos),
            "keys": list(_REPO_ENV_KEYS),
        },
        "demo": {"present": any(load_demo_cards().values())},
        "ready": bool(gh["ok"] and any_engine and repos),
    }


def list_owner_repos(limit: int = 100) -> dict[str, Any]:
    """List the operator's GitHub repos for the repo-pick checklist.

    Runs ``gh repo list --json nameWithOwner,...`` for the authenticated user
    (no org argument: the owner's own + accessible repos). Returns
    ``{repos: [{name_with_owner, description, is_private, is_fork, updated_at,
    selected}], selected, error?}``. Never raises: a gh/auth failure returns an
    ``error`` string with an empty repo list so the client shows a clear "sign
    in to GitHub first" state instead of crashing.
    """
    selected = set(selected_repos())
    gh = gh_auth_status()
    if not gh["ok"]:
        return {
            "repos": [],
            "selected": sorted(selected),
            "error": gh["detail"],
        }
    limit = max(1, min(int(limit), 200))
    rows = _gh_repo_list(limit)
    if rows is None:
        return {
            "repos": [],
            "selected": sorted(selected),
            "error": "Could not list your GitHub repos. Check gh auth status.",
        }
    repos: list[dict[str, Any]] = []
    visible: set[str] = set()
    for row in rows:
        slug = str(row.get("nameWithOwner") or "").strip()
        if not slug:
            continue
        normalized = slug.lower()
        visible.add(normalized)
        repos.append(
            {
                "name_with_owner": slug,
                "description": (row.get("description") or "").strip() or None,
                "is_private": bool(row.get("isPrivate")),
                "is_fork": bool(row.get("isFork")),
                "updated_at": row.get("updatedAt"),
                "selected": normalized in selected,
                "listed": True,
            }
        )
    for slug in sorted(selected - visible):
        repos.append(
            {
                "name_with_owner": slug,
                "description": "Already selected, but not returned by gh for this account.",
                "is_private": False,
                "is_fork": False,
                "updated_at": None,
                "selected": True,
                "listed": False,
            }
        )
    return {"repos": repos, "selected": sorted(selected)}


def _gh_repo_list(limit: int) -> list[dict[str, Any]] | None:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    successes = 0

    for cmd in _gh_repo_list_commands(limit):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env=_gh_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, list):
            continue
        successes += 1
        for row in data:
            if not isinstance(row, dict):
                continue
            slug = str(row.get("nameWithOwner") or "").strip().lower()
            if not slug or slug in seen:
                continue
            seen.add(slug)
            rows.append(row)
    return rows if successes else None


def _gh_repo_list_commands(limit: int) -> list[list[str]]:
    base = [
        _gh_bin(),
        "repo",
        "list",
        "--no-archived",
        "--limit",
        str(limit),
        "--json",
        "nameWithOwner,description,isPrivate,isFork,updatedAt",
    ]
    commands = [base]
    for owner in _repo_list_owners():
        commands.append([_gh_bin(), "repo", "list", owner, *base[3:]])
    return commands


def _repo_list_owners() -> list[str]:
    owners: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        owner = raw.strip().lower()
        if not owner or not re.match(r"^[a-z0-9_.-]+$", owner):
            return
        if owner in seen:
            return
        seen.add(owner)
        owners.append(owner)

    for raw in re.split(r"[\s,]+", config_value("GH_ORG") or ""):
        add(raw)
    for slug in selected_repos():
        owner, sep, _repo = slug.partition("/")
        if sep:
            add(owner)
    return owners


# --------------------------------------------------------------------------- #
# Starter playbooks
# --------------------------------------------------------------------------- #
# Canned overnight jobs. Picking one composes a concrete first request so the
# operator sees a real job, not a blank board. Each carries the structured
# IssueDraft fields the compose path expects (title/problem/desired/acceptance)
# so it threads through the same readiness scoring as a hand-typed request.
STARTER_PLAYBOOKS: list[dict[str, Any]] = [
    {
        "key": "triage-prs",
        "title": "Triage open PRs every night",
        "summary": (
            "Each night, review every open pull request and post a short triage "
            "note: what it changes, whether it looks ready, and what is blocking it."
        ),
        "draft": {
            "title": "Nightly: triage open pull requests",
            "problem": (
                "Open pull requests pile up without a quick read on which are "
                "ready to merge and which are stuck."
            ),
            "user": "Repo owner reviewing work each morning",
            "desired_behavior": (
                "Once a night, summarize each open PR (intent, readiness, "
                "blockers) so the morning review starts from a clear list."
            ),
            "acceptance_criteria": [
                "Every open PR has a one-line triage note.",
                "Blocked PRs are called out with the reason.",
            ],
        },
    },
    {
        "key": "fix-failing-ci",
        "title": "Fix failing CI",
        "summary": (
            "Find a pull request whose CI is failing, diagnose the failure, and "
            "open a fix so the branch goes green."
        ),
        "draft": {
            "title": "Fix a failing CI check",
            "problem": (
                "A pull request has a failing CI check and is blocked from merge until it is green."
            ),
            "user": "Repo owner waiting on a green build",
            "desired_behavior": (
                "Diagnose the failing check, apply the smallest correct fix, and "
                "push it so CI passes."
            ),
            "acceptance_criteria": [
                "The previously failing check passes.",
                "The fix is scoped to the failure, with no unrelated changes.",
            ],
        },
    },
    {
        "key": "tidy-readme",
        "title": "Refresh the README",
        "summary": (
            "Read the repo and bring its README up to date: setup steps, what the "
            "project does, and how to run it."
        ),
        "draft": {
            "title": "Refresh the README to match the code",
            "problem": (
                "The README has drifted from what the code actually does, so a "
                "newcomer cannot get started from it."
            ),
            "user": "A newcomer reading the repo for the first time",
            "desired_behavior": (
                "Update the README so the overview, setup steps, and run "
                "instructions match the current code."
            ),
            "acceptance_criteria": [
                "Setup and run steps work as written.",
                "The overview matches what the code does today.",
            ],
        },
    },
]


def playbook_by_key(key: str) -> dict[str, Any] | None:
    for playbook in STARTER_PLAYBOOKS:
        if playbook["key"] == key:
            return playbook
    return None


# --------------------------------------------------------------------------- #
# Demo board store
# --------------------------------------------------------------------------- #
def _demo_path(state_root: Path) -> Path:
    return Path(state_root) / _DEMO_FILENAME


def _demo_card(
    *,
    number: int,
    title: str,
    kind: str,
    column: str,
    age_days: int,
    now: datetime,
) -> dict[str, Any]:
    ts = now.isoformat()
    return {
        "repo": DEMO_REPO,
        "number": number,
        "title": title,
        # No URL: a demo card must never deep-link to a real GitHub page.
        "url": None,
        "author": "alfred-demo",
        "kind": kind,
        "timestamp": ts,
        "age_days": age_days,
        "is_draft": False,
        # The "demo" label is the client's render hook for the clearly-labelled
        # sample badge, so a demo card can never be mistaken for real work.
        "labels": ["demo"],
        "column": column,
        "demo": True,
    }


def _demo_template(now: datetime) -> dict[str, list[dict[str, Any]]]:
    return {
        "queued": [
            _demo_card(
                number=1001,
                title="[Demo] Add a dark-mode toggle to the settings page",
                kind="issue",
                column="queued",
                age_days=0,
                now=now,
            ),
        ],
        "in_progress": [
            _demo_card(
                number=1002,
                title="[Demo] Fix the flaky checkout integration test",
                kind="pr",
                column="in_progress",
                age_days=1,
                now=now,
            ),
        ],
        "shipped": [
            _demo_card(
                number=1003,
                title="[Demo] Speed up the dashboard initial load",
                kind="pr",
                column="shipped",
                age_days=2,
                now=now,
            ),
        ],
    }


def seed_demo(state_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Write a few clearly-labelled demo cards under the state root.

    Idempotent: re-seeding overwrites with a fresh-dated set so the demo never
    looks stale. The cards are local-only (never created on GitHub) and carry a
    ``demo`` flag + label so :func:`load_demo_cards` and the client can render
    and clear them unambiguously.
    """
    now = now or datetime.now(UTC)
    cards = _demo_template(now)
    payload = {
        "seeded_at": now.isoformat(),
        "columns": cards,
    }
    path = _demo_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    counts = {col: len(items) for col, items in cards.items()}
    return {"seeded": True, "counts": counts, "path": str(path)}


def clear_demo(state_root: Path) -> dict[str, Any]:
    """Remove the demo cards. Idempotent: a missing file is a clean clear."""
    path = _demo_path(state_root)
    removed = path.exists()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        removed = False
    return {"cleared": True, "removed": removed}


def load_demo_cards(state_root: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Read the persisted demo cards as ``{column: [card, ...]}``.

    Returns empty columns when no demo is seeded (or the file is unreadable),
    so :func:`shipped_board.build_board` can merge them with no branching.
    ``state_root`` defaults to ``$ALFRED_HOME/state`` so the board (which has no
    request context) can load them too.
    """
    empty: dict[str, list[dict[str, Any]]] = {
        "queued": [],
        "in_progress": [],
        "shipped": [],
    }
    if state_root is None:
        base = os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")
        state_root = Path(base) / "state"
    path = _demo_path(state_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(payload, dict):
        return empty
    columns = payload.get("columns")
    if not isinstance(columns, dict):
        return empty
    out = dict(empty)
    for col in empty:
        items = columns.get(col)
        if isinstance(items, list):
            out[col] = [item for item in items if isinstance(item, dict)]
    return out
