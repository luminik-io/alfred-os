"""
alfred-os — shared library for launchd-managed Claude Code agents.

Provides the primitives every codename agent needs:

- ``preflight()`` / ``doctor_mode()`` — fail loud and early on mis-configured hosts.
- ``with_lock()`` / ``AgentLock`` — mkdir-atomic per-agent mutex.
- ``SpendState`` — per-agent per-day firings / turns / cost / failure tracking.
- ``is_globally_blocked()`` / ``set_global_block()`` — fleet-wide rate-limit poison pill.
- ``run()`` / ``gh_json()`` — `subprocess` wrappers with sane defaults and clear errors.
- ``claude_invoke()`` — call ``claude -p`` and parse the structured result.
- ``codex_invoke()`` — optional ``codex exec`` subprocess for review-style tasks.
- ``make_worktree()`` / ``remove_worktree()`` — per-firing git-worktree isolation.
- ``slack_post()`` — webhook-based Slack notification with disk caching.
- ``ensure_labels()`` / ``gh_pr_create()`` / ``gh_issue_*()`` — gh CLI wrappers.

Consumers (e.g. luminik-io/alfred) write a thin role runner such as
``bin/lucius.py`` or ``bin/huntress.py`` that imports from this module,
declares a ``PreflightSpec``, and calls ``claude_invoke()``. The runner does
no LLM-orchestration work itself; all
real work happens inside a CLI subprocess against the operator's configured
Claude Code subscription, optional Codex login, or any wrapper binary they
configure.

Path defaults assume a single macOS host. ``HERMES_HOME`` defaults to
``~/.hermes`` and ``WORKSPACE_ROOT`` to ``~/code``; both are env-var
overridable. ``GH_ORG`` is required for any helper that targets GitHub
(e.g. ``gh_pr_create``); set it once in the launchd plist's
``EnvironmentVariables`` block.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Path resolution
#
# Two env vars override the defaults so a fresh user can clone this repo
# and run it without editing source. Both fall back to the Mac-Mini layout
# the agents originally shipped on, which means existing deployments need
# zero migration.
#
#   HERMES_HOME       - where the agent runtime lives (state/, worktrees/,
#                       lib/, bin/, shared/.agent/). Defaults to ~/.hermes.
#   WORKSPACE_ROOT    - root of the per-repo product checkouts (every
#                       <repo> in GH_REPO_TO_LOCAL is a child directory).
#                       Defaults to ~/code.
#   CLAUDE_BIN        - absolute path to the `claude` CLI. Defaults to
#                       whatever is on $PATH; override only if you have a
#                       non-standard install. Set to a fully-qualified path
#                       on hosts without `claude` on PATH (e.g., launchd).
#   CODEX_BIN         - absolute path to the `codex` CLI. Defaults to
#                       whatever is on $PATH. Only used by llm-tier:codex.
# --------------------------------------------------------------------------
HOME = Path(os.path.expanduser("~"))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT") or os.path.expanduser("~/code"))

# Back-compat alias: alfred-era bin scripts import WORKSPACE and use it as
# the root containing per-repo checkouts under product/. Keep that shape.
# New consumers can ignore this and reference WORKSPACE_ROOT directly.
HERMES = HERMES_HOME
WORKSPACE = WORKSPACE_ROOT / "product"

# GitHub org/user slug for repo-targeting helpers (gh_pr_create, gh_issue_*).
# Required only when those helpers are used; agents that don't touch gh can
# leave it unset. Setting it once in the launchd plist EnvironmentVariables
# block is the canonical configuration site.
GH_ORG = os.environ.get("GH_ORG", "").strip()

# Convenience constants used by the bin/ scripts
STATE_ROOT = HERMES_HOME / "state"
WORKTREE_ROOT = HERMES_HOME / "worktrees"
WORKTREES_ROOT = WORKTREE_ROOT  # plural alias matching docs / launchd discussion
LIB_DIR = HERMES_HOME / "lib"
BIN_DIR = HERMES_HOME / "bin"
TRANSCRIPTS_ROOT = STATE_ROOT / "transcripts"
PROMPTS_ROOT = HERMES_HOME / "prompts"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CODEX_DEFAULT_MODEL = os.environ.get("CODEX_MODEL", "").strip() or None
CODEX_DEFAULT_SANDBOX = os.environ.get("CODEX_SANDBOX", "read-only").strip() or "read-only"
CODEX_APPROVAL_POLICY = os.environ.get("CODEX_APPROVAL_POLICY", "never").strip() or "never"
CODEX_TRANSCRIPTS_ROOT = STATE_ROOT / "codex"
ENGINE_CHOICES = {"claude", "codex", "hybrid"}
SLACK_WEBHOOK_CACHE = STATE_ROOT / "slack-webhook.cache"
SLACK_WEBHOOK_CACHE_TTL = 30 * 24 * 3600  # 30 days; the webhook URL itself is stable

# Shared rate-limit blocker — when ANY agent hits Anthropic's error_rate_limit
# or error_budget, all agents respect the block until the timeout passes.
# Otherwise each scheduled agent would keep firing into the rate-limit wall.
GLOBAL_BLOCKED_FILE = STATE_ROOT / "global-blocked-until.json"


def is_globally_blocked() -> str | None:
    """Return reason string if a global rate-limit block is active, else None."""
    if not GLOBAL_BLOCKED_FILE.exists():
        return None
    try:
        data = json.loads(GLOBAL_BLOCKED_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return None
    until = data.get("until", "")
    try:
        exp = datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    if datetime.now(UTC) >= exp:
        with contextlib.suppress(OSError):
            GLOBAL_BLOCKED_FILE.unlink()
        return None
    return f"global rate-limit block until {until} (reason: {data.get('reason', 'unknown')})"


def set_global_block(hours: int, reason: str) -> str:
    """Set a global rate-limit block. Returns the until-iso string."""
    from datetime import timedelta

    until = (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    GLOBAL_BLOCKED_FILE.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_BLOCKED_FILE.write_text(json.dumps({"until": until, "reason": reason}))
    return until


PROVIDER_LIMIT_SUBTYPES = {"error_budget", "error_rate_limit"}


def normalize_engine(raw: str | None, *, default: str = "hybrid") -> str:
    """Normalize an agent engine mode."""
    value = (raw or "").strip().lower()
    if value == "both":
        return "hybrid"
    if value in ENGINE_CHOICES:
        return value
    fallback = (default or "hybrid").strip().lower()
    if fallback == "both":
        return "hybrid"
    return fallback if fallback in ENGINE_CHOICES else "hybrid"


def _agent_env_slug(agent: str) -> str:
    return agent.strip().upper().replace("-", "_")


def agent_engine(
    agent: str,
    *,
    default: str = "hybrid",
    legacy_env: str | None = None,
    legacy_state_file: Path | None = None,
    environ: dict[str, str] | None = None,
) -> str:
    """Resolve the configured engine for one agent.

    Precedence:
    1. ``ALFRED_<AGENT>_ENGINE``
    2. optional legacy env var, such as ``ALFRED_REVIEW_ENGINE``
    3. ``ALFRED_ENGINE`` for fleet-wide testing
    4. ``${HERMES_HOME}/state/engines/<agent>``
    5. optional legacy state file
    6. default
    """
    env = environ if environ is not None else os.environ
    safe_agent = agent.strip().lower().replace("_", "-")
    env_name = f"ALFRED_{_agent_env_slug(safe_agent)}_ENGINE"
    for name in (env_name, legacy_env, "ALFRED_ENGINE"):
        if name and env.get(name, "").strip():
            return normalize_engine(env.get(name), default=default)

    state_file = STATE_ROOT / "engines" / safe_agent
    for path in (state_file, legacy_state_file):
        if not path:
            continue
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw:
            return normalize_engine(raw, default=default)
    return normalize_engine(None, default=default)


def engine_preflight_bins(engine: str, *, hybrid_requires_codex: bool = False) -> list[str]:
    """Return load-bearing binaries for an engine mode.

    Hybrid is Claude-first by default, so a missing optional Codex fallback
    does not stop ordinary scheduled work.
    """
    mode = normalize_engine(engine)
    if mode == "codex":
        return [CODEX_BIN]
    if mode == "hybrid" and hybrid_requires_codex:
        return [CLAUDE_BIN, CODEX_BIN]
    return [CLAUDE_BIN]


def maybe_set_global_block_for_result(
    agent: str,
    result: Any,
    *,
    hours: int = 1,
    engine_used: str | None = "claude",
) -> str | None:
    if engine_used != "claude":
        return None
    subtype = getattr(result, "subtype", "")
    if subtype not in PROVIDER_LIMIT_SUBTYPES:
        return None
    return set_global_block(hours=hours, reason=f"{agent}-{subtype}")


# GH_REPO_TO_LOCAL maps GitHub-repo-slug → local-checkout-directory under
# WORKSPACE_ROOT. Empty by default; consumers populate it for their fleet:
#
#     from agent_runner import GH_REPO_TO_LOCAL
#     GH_REPO_TO_LOCAL.update({
#         "myorg-backend": "backend",
#         "myorg-frontend": "frontend",
#     })
#
# Used by ``make_worktree(repo_slug, ...)`` to resolve where the checkout
# lives. If a slug isn't in the map the helper falls back to the slug name
# itself (so ``"backend"`` resolves to ``WORKSPACE_ROOT/product/backend``).
GH_REPO_TO_LOCAL: dict[str, str] = {}

# STANDARD_LABELS — labels that ``ensure_labels()`` will create on the
# target repo if missing. Each tuple: (name, hex-color-no-hash, description).
# Consumers can ``STANDARD_LABELS.extend(...)`` from their bin/*.py to add
# fleet-specific labels.
#
# The defaults below ship the labels Batman + the bundle model rely on so
# every PR-create / issue-edit path "just works" on a fresh product repo:
#
#   batman-pr-open       — set by Batman when a bundle PR exists in the
#                          repo; cleared on merge.
#   agent:large-feature  — issue label that opts the issue into Batman's
#                          bundle search (multi-repo feature work).
#
# Without these defaults, the first PR-create call against a fresh repo
# would fail with "could not add label" and return None silently; the
# operator then got "PR open failed" with no breadcrumb. (Same root
# cause as luminik-io/alfred Issue #142.)
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
]


def _full_repo(slug: str) -> str:
    """Resolve a bare repo slug to ``<org>/<repo>`` using the GH_ORG env var.

    If the input already contains a ``/`` it's treated as a full slug and
    returned unchanged. If GH_ORG is unset and the input is bare, raise
    RuntimeError so the caller fails loud rather than calling gh with a
    half-formed target.
    """
    if "/" in slug:
        return slug
    if not GH_ORG:
        raise RuntimeError(
            f"GH_ORG env var is unset; cannot resolve bare repo slug '{slug}' "
            "to <org>/<repo>. Set GH_ORG in your launchd plist or pass full "
            "slug like 'myorg/myrepo'."
        )
    return f"{GH_ORG}/{slug}"


# ---------- Helpers ----------


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    """Read a small integer knob from env without letting bad values break scheduled runs.

    Missing / non-integer values fall back to ``default``. Result is always
    clamped to the ``[minimum, maximum]`` range — including the fallback
    path — so a typo in the launchd plist can't kneecap or unbound a
    per-firing budget. A caller that supplies an out-of-range ``default``
    (e.g. ``default`` above ``maximum``) gets the clamped value, not the
    raw default; the safety guarantee is unconditional.
    """
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = default
    else:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def optional_env_int(name: str, *, minimum: int = 1, maximum: int | None = None) -> int | None:
    """Read an optional integer knob; return None when unset or unparseable.

    Designed for "no default ceiling, but allow temporary debugging via env"
    knobs — most prominently the per-firing ``max_turns`` budget on agents
    where a hard cap produces no-output runs (the agent burns its whole
    budget surveying without ever reaching a sentinel). Callers pass the
    result straight to ``claude_invoke_streaming(max_turns=...)``; when
    None the streaming wrapper maps it to ``_CLAUDE_UNLIMITED_TURNS`` so
    the Claude CLI never falls back to its hidden 40-turn default. The
    firing-level ``timeout`` remains the real bound.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


# Claude Code's ``-p`` (non-interactive) mode applies a hidden 40-turn
# default when ``--max-turns`` is omitted. That default is far too tight
# for our agents — Lucius routinely needs 60-150 turns on cross-file work,
# Drake's healthy planning runs hit 60+. ``claude_invoke`` and
# ``claude_invoke_streaming`` always pass an explicit ``--max-turns``: the
# caller's value if given, otherwise this effectively-unlimited number,
# so the per-firing wall-clock ``timeout`` becomes the real ceiling.
# Callers that genuinely want a low cap pass ``max_turns=<int>`` themselves;
# the env-knob path (``ALFRED_<AGENT>_MAX_TURNS`` via ``optional_env_int``)
# feeds into the same parameter.
_CLAUDE_UNLIMITED_TURNS = 999


def run(
    cmd: list[str],
    *,
    cwd: str | None = None,
    timeout: int = 60,
    check: bool = False,
    capture: bool = True,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Wrapped subprocess.run with sane defaults + clear errors."""
    proc_env = dict(os.environ)
    if env:
        proc_env.update(env)
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            timeout=timeout,
            check=check,
            capture_output=capture,
            text=True,
            env=proc_env,
        )
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(
            cmd, 124, stdout=e.stdout or "", stderr=f"TIMEOUT after {timeout}s"
        )
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"{type(e).__name__}: {e}")


def gh_json(cmd: list[str], default: Any = None) -> Any:
    """Run `gh` and parse JSON; return default on any failure."""
    res = run(cmd, timeout=60)
    if res.returncode != 0:
        return default
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return default


SLACK_SEVERITY_INFO = "info"
SLACK_SEVERITY_WARN = "warn"
SLACK_SEVERITY_ALERT = "alert"
_SLACK_SEVERITIES = {SLACK_SEVERITY_INFO, SLACK_SEVERITY_WARN, SLACK_SEVERITY_ALERT}


def slack_post(text: str, *, severity: str = SLACK_SEVERITY_INFO) -> bool:
    """Post to a Slack webhook. Returns True on confirmed POST.

    Webhook URL resolution, in order:

    1. ``SLACK_WEBHOOK_URL`` env var — simplest path; set it once in your
       launchd plist or shell profile.
    2. Disk cache at ``${HERMES_HOME}/state/slack-webhook.cache`` (30-day TTL)
       — written by step 3 the first time it succeeds, so subsequent calls
       skip the AWS round-trip.
    3. AWS Secrets Manager — secret id from ``SLACK_WEBHOOK_SECRET_ID``
       (default ``alfred/slack-webhook``), region from
       ``SLACK_WEBHOOK_SECRET_REGION`` (default ``us-east-1``). Optional;
       lets you keep the URL out of plain env if you've already wired AWS.

    Severity routing (``severity=`` keyword, default ``info``):

      ``info``   Posted as-is. The bulk of fleet telemetry — agent shipped,
                 merged, swept, no-op. Threading-by-day is deferred until
                 a bot token integration ships (incoming webhooks cannot
                 post threaded replies — that requires
                 ``chat.postMessage`` with a ``xoxb-`` token + ``thread_ts``).
      ``warn``   Prefixed with ⚠️ if not already. Use for: rate-limit hit
                 on one provider, max-turns hit, soft-failure, salvaged
                 partial work.
      ``alert``  Prefixed with 🚨 and appends ``<!here>`` so channel
                 members get pinged. Use sparingly: production drift,
                 fleet-wide rate-limit, doctor failure on a load-bearing
                 agent, security signal.

    Unknown severity values are coerced to ``info``. Existing callers
    that don't pass ``severity=`` keep their previous behaviour exactly.

    Returns False on empty text, missing webhook, or any HTTP error.
    Callers that need at-least-once semantics read the return value; pure
    fire-and-forget callers ignore it.
    """
    text = (text or "").strip()
    if not text:
        return False
    if severity not in _SLACK_SEVERITIES:
        severity = SLACK_SEVERITY_INFO
    if severity == SLACK_SEVERITY_WARN:
        if not text.startswith(("⚠️", "❌", "⏸️")):
            text = f"⚠️  {text}"
    elif severity == SLACK_SEVERITY_ALERT:
        if not text.startswith("🚨"):
            text = f"🚨 {text}"
        if "<!here>" not in text and "<!channel>" not in text:
            text = f"{text}\n<!here>"
    # Slack truncates at 3500 chars
    if len(text) > 3500:
        text = text[:3500] + "\n...[truncated]"

    # 1. Env var (most explicit, used by anyone not running AWS)
    hook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

    # 2. Disk cache from a prior successful resolution
    if not hook and SLACK_WEBHOOK_CACHE.exists():
        age = time.time() - SLACK_WEBHOOK_CACHE.stat().st_mtime
        if age < SLACK_WEBHOOK_CACHE_TTL:
            hook = SLACK_WEBHOOK_CACHE.read_text().strip()

    # 3. AWS Secrets Manager fallback
    if not hook:
        secret_id = os.environ.get("SLACK_WEBHOOK_SECRET_ID", "alfred/slack-webhook")
        secret_region = os.environ.get("SLACK_WEBHOOK_SECRET_REGION", "us-east-1")
        res = run(
            [
                "aws",
                "secretsmanager",
                "get-secret-value",
                "--secret-id",
                secret_id,
                "--region",
                secret_region,
                "--query",
                "SecretString",
                "--output",
                "text",
            ],
            timeout=8,
        )
        if res.returncode != 0 or not res.stdout.strip():
            # Silently skip — don't flood stderr on every call when Slack is
            # unconfigured. Callers that need at-least-once read the False return.
            return False
        hook = res.stdout.strip()
        try:
            SLACK_WEBHOOK_CACHE.parent.mkdir(parents=True, exist_ok=True)
            SLACK_WEBHOOK_CACHE.write_text(hook)
            SLACK_WEBHOOK_CACHE.chmod(0o600)
        except OSError:
            pass

    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(hook, data=payload, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return True
    except Exception as e:
        print(f"[slack-post] error: {type(e).__name__}: {e}", file=sys.stderr)
        return False


# ---------- Preflight ----------
#
# Every agent invokes preflight() right after acquiring its lock. The point is
# to fail loud and early when the host is mis-configured: missing env vars,
# stale gh auth, dead AWS profile, missing CLI binary, missing local checkout.
#
# Without this, agents would burn a Claude turn on a request that was always
# going to crash in the middle (e.g. `gh issue create` with expired auth) and
# leave half-finished side effects. With it, the firing exits in milliseconds
# with a single Slack line naming the missing piece.
#
# doctor.sh sets HERMES_DOCTOR=1 and runs every agent so the operator can
# verify a fresh setup without burning Claude turns.


@dataclass
class PreflightSpec:
    """Declarative requirements for an agent run.

    All fields are optional: an agent that needs nothing more than `claude`
    on PATH and the two workspace env vars can declare just `bins=["claude"]`.
    """

    agent: str
    env_vars: list[str] = field(default_factory=lambda: ["HERMES_HOME", "WORKSPACE_ROOT"])
    bins: list[str] = field(default_factory=list)
    aws_profile: str | None = None
    require_gh_auth: bool = False
    require_workspace_repos: list[str] = field(default_factory=list)


class PreflightFailed(RuntimeError):
    """Raised by preflight() when one or more checks fail.

    The caller catches and exits 0 cleanly; preflight() has already posted a
    one-line Slack message and printed a sentinel to stdout.
    """


def _env_value_enabled(name: str) -> bool:
    value = os.environ.get(name)
    return bool(value and value.strip().lower() not in {"", "0", "false", "no", "off"})


def preflight(spec: PreflightSpec) -> None:
    """Validate the host before doing real work. Raise PreflightFailed on miss.

    Reports every miss in one shot rather than one-at-a-time so the operator
    sees the full picture in a single Slack notification.
    """
    import shutil  # local import: only used when an agent actually checks bins

    misses: list[str] = []

    # 1. Required env vars. HERMES_HOME / WORKSPACE_ROOT default to user-home
    #    paths so missing env is rare on the canonical Mac Mini; a fresh fork
    #    on Linux or in a container will surface it here.
    for var in spec.env_vars:
        if not os.environ.get(var):
            misses.append(f"env var `{var}` is unset")

    # 2. Required CLI binaries on PATH. Anything with a `/` is treated as a
    #    fully-qualified path and checked for executability.
    for binname in spec.bins:
        if "/" in binname:
            if not Path(binname).is_file() or not os.access(binname, os.X_OK):
                misses.append(f"binary `{binname}` is not an executable file")
        elif not shutil.which(binname):
            misses.append(f"binary `{binname}` not found on PATH")

    # 3. AWS profile usable. Strip inherited keys so we never accidentally use
    #    the operator's interactive SSO session in place of the agent's
    #    scoped IAM user.
    if spec.aws_profile:
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_SECURITY_TOKEN",
            )
        }
        env["AWS_PROFILE"] = spec.aws_profile
        try:
            sts = subprocess.run(
                ["aws", "sts", "get-caller-identity", "--query", "Arn", "--output", "text"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            misses.append(f"AWS profile `{spec.aws_profile}` check timed out")
        except FileNotFoundError:
            misses.append("binary `aws` not found on PATH")
        else:
            out = (sts.stderr or sts.stdout or "").strip()
            if sts.returncode != 0 or spec.aws_profile not in (sts.stdout or ""):
                err = out.splitlines()[-1] if out else "no output"
                misses.append(f"AWS profile `{spec.aws_profile}` not usable: {err[:120]}")

    # 4. gh auth alive — every issue / PR / label operation needs it.
    if spec.require_gh_auth:
        try:
            gh = subprocess.run(
                ["gh", "auth", "status"], capture_output=True, text=True, timeout=30
            )
        except subprocess.TimeoutExpired:
            misses.append("gh auth status timed out")
        except FileNotFoundError:
            misses.append("binary `gh` not found on PATH")
        else:
            if gh.returncode != 0:
                misses.append("gh auth not active (run `gh auth login`)")

    # 5. Local repo checkouts present. Agents that grep across a repo can't
    #    do useful work if the checkout is missing.
    for repo in spec.require_workspace_repos:
        repo_path = WORKSPACE_ROOT / "product" / repo
        if not (repo_path / ".git").exists():
            misses.append(f"checkout `{repo_path}` missing or not a git repo")

    if not misses:
        return

    sentinel = f"[{spec.agent.upper()}-PREFLIGHT-FAILED]"
    detail = "\n  ".join(f"- {m}" for m in misses)
    print(f"{sentinel} {len(misses)} issue(s):\n  {detail}")
    headline = misses[0] + (f" (+{len(misses) - 1} more)" if len(misses) > 1 else "")
    suppress_slack = (
        _env_value_enabled("HERMES_DOCTOR")
        or spec.agent == "test"
        or (
            not _env_value_enabled("XPC_SERVICE_NAME")
            and not _env_value_enabled("ALFRED_PREFLIGHT_FORCE_SLACK")
        )
    )
    if not suppress_slack:
        slack_post(f"🚫 {spec.agent} preflight failed: {headline}")
    raise PreflightFailed(misses)


def doctor_mode() -> bool:
    """True when running under doctor.sh (HERMES_DOCTOR=1).

    Agents check this after preflight passes and exit 0 with a [<AGENT>-DOCTOR-OK]
    sentinel instead of doing real work. Lets the operator verify a fresh setup
    without burning Claude turns or making side effects.
    """
    return _env_value_enabled("HERMES_DOCTOR")


# ---------- Prompt loading + variable substitution ----------
#
# Codename agents read their system prompt from a markdown file in the repo.
# The same file is editable as documentation AND consumed by the runner at
# firing time. To keep operator-specific
# values out of the file (gh handle, email, repo lists) without forcing a
# pre-render step, load_prompt() does shell-style ${VAR} substitution
# against the process env when reading the file.
#
# Unset variables are left as literal ${VAR}. That's deliberate: a missing
# OPERATOR_GH_HANDLE shouldn't crash the agent or silently substitute an
# empty string into a `gh` call. Use ``preflight()`` with the env var in
# ``env_vars`` to fail loud on missing config.


def load_prompt(path: Path | str, *, extra_vars: dict[str, str] | None = None) -> str:
    """Read a prompt file and substitute ``${VAR}`` placeholders from env.

    ``extra_vars`` overrides ``os.environ`` for specific keys; useful for
    per-firing context like ``${ISSUE_NUMBER}`` or ``${REPO_SLUG}``.
    """
    import string

    p = Path(path)
    text = p.read_text()
    mapping = dict(os.environ)
    if extra_vars:
        mapping.update(extra_vars)
    return string.Template(text).safe_substitute(mapping)


# ---------- Role / codename metadata ----------
#
# Codenames alone don't carry meaning. A fresh contributor (or your future
# self at 2am) reads ``[BATMAN-PLAN-DRAFTED]`` or ``[NIGHTWING-COMPLETE]``
# and has to cross-reference the per-agent prompt to figure out what the
# agent does. ``role`` is a one-line operational descriptor stored in
# ``agents.conf`` column 7 and rendered into each launchd plist as
# ``ALFRED_<CODENAME>_ROLE``. Slack post prefixes and the ``alfred agents``
# CLI surface ``codename (role)`` so codenames stay decorative without
# losing operational context.


def agent_role(codename: str) -> str:
    """Return the one-line operational role descriptor for an agent.

    Read from ``ALFRED_<CODENAME>_ROLE`` (rendered into each launchd plist
    by ``launchd/render.sh`` from agents.conf column 7). Returns the empty
    string when no role is set; never raises. ``-`` characters in compound
    codenames (``alfred-nightly``, ``brand-mention-scanner``) are
    translated to ``_`` to match what render.sh emits.
    """
    if not codename:
        return ""
    env_key = "ALFRED_" + codename.upper().replace("-", "_") + "_ROLE"
    return (os.environ.get(env_key) or "").strip()


def codename_with_role(codename: str) -> str:
    """Format ``"<codename> (<role>)"`` when a role is set, else the bare codename.

    Slack post prefixes and CLI status output use this so a reader who
    hasn't memorized the agent cast still gets operational context next
    to every codename.
    """
    role = agent_role(codename)
    return f"{codename} ({role})" if role else codename


# ---------- Per-firing event log (jsonl) ----------
#
# An append-only structured event log per agent firing. Solves three pains:
# (1) reconstructing what an agent did in a Slack post-mortem, (2) feeding
# cost/usage analytics off-line without re-parsing claude transcripts,
# (3) cheap pattern adoption from OpenHands EventStream + smolagents
# MemoryStep.
#
# The record shape is intentionally loose: any JSON-serialisable dict goes.
# Every record gets ``ts`` (UTC ISO) and ``agent`` injected automatically.


class EventLog:
    """Append-only JSONL log for a single firing.

    Usage:

        events = EventLog(agent="lucius", firing_id="2026-04-29-1647-bf3a")
        events.emit("preflight_passed")
        events.emit("issue_picked", repo="myorg/backend", number=275)
        ...
        events.emit("pr_opened", url=pr_url, files_changed=12)
    """

    def __init__(self, agent: str, firing_id: str | None = None, path: Path | None = None) -> None:
        self.agent = agent
        # firing_id defaults to a UTC-stamped + short-random tag; keep it
        # short enough to fit in a git-commit trailer and a slack message.
        if firing_id is None:
            import secrets

            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            firing_id = f"{stamp}-{secrets.token_hex(2)}"
        self.firing_id = firing_id
        if path is None:
            d = STATE_ROOT / agent / "events"
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{firing_id}.jsonl"
        self.path = path

    def emit(self, event: str, **fields: Any) -> None:
        """Append one record. Never raises; a broken event log shouldn't
        kill an agent firing — print to stderr and continue."""
        record = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "agent": self.agent,
            "firing_id": self.firing_id,
            "event": event,
            **fields,
        }
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except OSError as e:
            print(f"[event-log] write failed: {e}", file=sys.stderr)


# ---------- Commit trailer for codename + firing-id provenance ----------
#
# Borrowed from aider's "aider made this" trailer convention. Every commit
# an agent makes carries a structured trailer naming the codename + the
# firing that produced it. Makes forensic queries trivial:
#
#   git log --grep "Agent-Codename: lucius" --since="last week"
#   git log --grep "Agent-Firing-Id: 2026-04-29-1647-bf3a"


def commit_trailer(agent: str, firing_id: str, *, extra: dict[str, str] | None = None) -> str:
    """Build a multi-line commit-trailer block.

    Caller appends this to their commit message. Format follows the
    'Trailer: Value' convention git itself uses for `Co-Authored-By` etc.,
    so existing tooling (`git interpret-trailers`) parses it correctly.
    """
    lines = [
        f"Agent-Codename: {agent}",
        f"Agent-Firing-Id: {firing_id}",
    ]
    if extra:
        for k, v in extra.items():
            # Trailer keys are PascalCased by convention.
            key = "-".join(part.capitalize() for part in k.replace("_", "-").split("-"))
            lines.append(f"{key}: {v}")
    return "\n".join(lines)


# ---------- Hand-off table for codename routing ----------
#
# Borrowed from OpenAI swarm's Result(agent=NextAgent) pattern, but stored
# as data rather than buried in prompts. Lets ``doctor.sh`` validate that
# every advertised hand-off has both an emitter and a consumer; lets a
# quick `python3 -c "from agent_runner import HANDOFFS; print(HANDOFFS)"`
# answer "what can the fleet do?".
#
# Each entry: (from-codename, outcome-name) -> to-codename. Outcomes are
# free-form strings the emitter writes into its event log; the consumer
# uses them to filter what's relevant.
#
#     HANDOFFS.add("drake", "issue_filed", "lucius")
#     HANDOFFS.add("lucius", "pr_opened", "rasalghul")
#     HANDOFFS.add("rasalghul", "review_p1_unaddressed", "nightwing")


@dataclass
class HandoffTable:
    """Static codename routing map.

    Intentionally minimal: just a triple-keyed dict and validation helpers.
    The actual routing happens via labels and gh state, not via in-process
    calls — this struct is documentation + a shape doctor.sh can inspect.
    """

    edges: dict[tuple[str, str], str] = field(default_factory=dict)

    def add(self, from_agent: str, outcome: str, to_agent: str) -> None:
        self.edges[(from_agent, outcome)] = to_agent

    def consumers(self, codename: str) -> list[str]:
        """Outcomes the given codename emits that route to another codename."""
        return [outcome for (src, outcome), _ in self.edges.items() if src == codename]

    def producers(self, codename: str) -> list[tuple[str, str]]:
        """(from-agent, outcome) pairs that route to the given codename."""
        return [(src, outcome) for (src, outcome), dst in self.edges.items() if dst == codename]

    def validate(self, known_codenames: set[str]) -> list[str]:
        """Return list of issues — orphan emitters / consumers / unknown agents."""
        misses: list[str] = []
        for (src, outcome), dst in self.edges.items():
            if src not in known_codenames:
                misses.append(f"hand-off from unknown agent '{src}' (outcome={outcome})")
            if dst not in known_codenames:
                misses.append(f"hand-off to unknown agent '{dst}' (outcome={outcome})")
        return misses


# Module-level singleton; consumers extend at import time.
HANDOFFS = HandoffTable()


# ---------- Lock ----------


@dataclass
class AgentLock:
    """Mutex via mkdir(2) atomicity. Auto-released on process exit."""

    name: str
    _lock_dir: Path = field(init=False)

    def __post_init__(self):
        self._lock_dir = Path(f"/tmp/agent-lock-{self.name}")

    def acquire(self) -> bool:
        try:
            self._lock_dir.mkdir(exist_ok=False)
        except FileExistsError:
            pid_file = self._lock_dir / "pid"
            try:
                old_pid = int(pid_file.read_text().strip())
                # Check if still alive
                os.kill(old_pid, 0)
                # alive — refuse
                return False
            except (FileNotFoundError, ValueError, ProcessLookupError):
                # Stale lock — try to clean and retry, but use exist_ok=False on
                # the retry so two concurrent processes can't both succeed.
                import shutil

                shutil.rmtree(self._lock_dir, ignore_errors=True)
                try:
                    self._lock_dir.mkdir(exist_ok=False)
                except FileExistsError:
                    # Another process won the race
                    return False
        pid = os.getpid()
        (self._lock_dir / "pid").write_text(str(pid))
        metadata = {
            "pid": pid,
            "pid_start_key": pid_start_key(pid),
            "cmdline": " ".join(sys.argv),
            "agent": self.name,
        }
        (self._lock_dir / "metadata.json").write_text(json.dumps(metadata, sort_keys=True))
        return True

    def release(self) -> None:
        import shutil

        shutil.rmtree(self._lock_dir, ignore_errors=True)


# ---------- Spend state ----------


@dataclass
class SpendState:
    """Per-agent per-day spend tracking. Auto-resets at midnight via per-day filename."""

    agent: str
    state: dict = field(default_factory=dict)
    _path: Path = field(init=False)

    def __post_init__(self):
        d = STATE_ROOT / self.agent
        d.mkdir(parents=True, exist_ok=True)
        self._path = d / f"spend-{today_str()}.json"
        if self._path.exists():
            try:
                self.state = json.loads(self._path.read_text())
            except json.JSONDecodeError:
                self.state = {}
        # Defaults
        self.state.setdefault("firings_today", 0)
        self.state.setdefault("turns_today", 0)
        self.state.setdefault("cost_usd_today", 0.0)
        self.state.setdefault("successes_today", 0)
        self.state.setdefault("failures_today", 0)
        self.state.setdefault("blocked_until", None)
        self.state.setdefault("last_session_id_per_target", {})
        self.state.setdefault("consecutive_failures", 0)

    def save(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.rename(self._path)

    def increment(self, **kwargs) -> None:
        for k, v in kwargs.items():
            self.state[k] = self.state.get(k, 0) + v
        self.save()

    def set(self, **kwargs) -> None:
        self.state.update(kwargs)
        self.save()

    def is_blocked(self) -> str | None:
        """Return reason if this agent should not fire, else None."""
        until = self.state.get("blocked_until")
        if until:
            try:
                exp = datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                if datetime.now(UTC) < exp:
                    return f"blocked until {until}"
                # Expired — clear
                self.state["blocked_until"] = None
                self.save()
            except ValueError:
                self.state["blocked_until"] = None
                self.save()
        return None


# ---------- Label management ----------


def ensure_labels(repo_slug: str, labels: list[tuple[str, str, str]] | None = None) -> None:
    """Idempotent label creation. Silent on already-exists. Cached per process."""
    if labels is None:
        labels = STANDARD_LABELS
    cache_key = f"_ensure_labels_done_{repo_slug}"
    if globals().get(cache_key):
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
    globals()[cache_key] = True


# ---------- Worktree ----------


def make_worktree(
    local_repo: str, agent: str, target: str, base: str = "origin/main"
) -> tuple[Path, str]:
    """Create a fresh worktree on a unique branch. Returns (path, branch)."""
    repo_path = WORKSPACE / local_repo
    ts = int(time.time())
    branch = f"{agent}/{target}-{ts}"
    wt = WORKTREE_ROOT / f"eng-{agent}-{local_repo}-{target}-{ts}"
    WORKTREE_ROOT.mkdir(exist_ok=True)
    run(["git", "fetch", "origin", "main"], cwd=str(repo_path), timeout=60)
    res = run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            branch,
            str(wt),
            base,
        ],
        cwd=str(repo_path),
        timeout=60,
    )
    if res.returncode != 0:
        raise RuntimeError(f"worktree add failed: {res.stderr.strip()}")
    return wt, branch


def make_worktree_from_branch(local_repo: str, agent: str, head_ref: str, target: str) -> Path:
    """Create a worktree pointing at an existing remote branch (read-only review use case)."""
    repo_path = WORKSPACE / local_repo
    ts = int(time.time())
    wt = WORKTREE_ROOT / f"eng-{agent}-{local_repo}-{target}-{ts}"
    WORKTREE_ROOT.mkdir(exist_ok=True)
    run(["git", "fetch", "origin", head_ref], cwd=str(repo_path), timeout=60)
    res = run(
        ["git", "worktree", "add", str(wt), f"origin/{head_ref}"], cwd=str(repo_path), timeout=60
    )
    if res.returncode != 0:
        raise RuntimeError(f"worktree add failed: {res.stderr.strip()}")
    return wt


def remove_worktree(local_repo: str, wt: Path) -> None:
    repo_path = WORKSPACE / local_repo
    run(["git", "worktree", "remove", "--force", str(wt)], cwd=str(repo_path), timeout=30)


def find_existing_worktree(local_repo: str, agent: str, target: str) -> Path | None:
    """Locate a previous-firing worktree for ``(agent, local_repo, target)``.

    Returns the most recent matching path under ``WORKTREE_ROOT`` or
    ``None`` if no leftover worktree exists. The glob pattern matches
    the on-disk naming convention written by ``make_worktree``:
    ``eng-<agent>-<repo>-<target>-<ts>``. Sorting by mtime keeps the
    dedup deterministic when (rare) more than one stale worktree
    exists for the same target.
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
    """Heuristic: is this worktree's branch detached from current ``base``?

    A worktree is stale when its branch is on ``HEAD`` (detached) OR
    when base has moved past it AND it has no commits ahead. The first
    half catches a wedged worktree we cannot resume on; the second
    catches the common case where the planner reset the issue label
    after a no-commit firing and main has since moved on. We always
    reuse a worktree that is ahead of base by any amount so prior work
    survives across firings.
    """
    repo_path = WORKSPACE / local_repo
    # Refresh local view of base so the comparison is honest.
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
    when we landed on a leftover worktree from a prior firing. Closes
    the runner-side dedup hole: every firing of a long-running issue
    (max-turns, partial commits) lands on the SAME worktree, so commits
    stack up and pre-push checks see the full state instead of a
    clean slate.

    Stale worktrees (``_worktree_is_stale``) are removed before the
    ``make_worktree`` fallback so ``WORKTREE_ROOT`` does not accumulate
    dead branches on every firing miss.
    """
    existing = find_existing_worktree(local_repo, agent, target)
    if existing is None:
        wt, branch = make_worktree(local_repo, agent, target, base=base)
        return wt, branch, False
    if _worktree_is_stale(local_repo, existing, base=base):
        # Best-effort cleanup. ``remove_worktree`` swallows failures
        # because git's --force already handles the locked / dirty
        # case; if even that fails we walk over to make_worktree which
        # uses a fresh ts and lands at a different path.
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
    # Reuse: refresh local view of main inside the worktree so the
    # resumed firing sees the latest base; the caller decides whether
    # to rebase. Best-effort fetch keeps the path clean if the network
    # is briefly unavailable.
    run(["git", "fetch", "origin", "main"], cwd=str(existing), timeout=60)
    return existing, branch, True


# ---------- Claude invocation ----------

# stop_reason discipline (ported from pi-mono):
#   - "end_turn"      : assistant finished cleanly
#   - "tool_use"      : assistant stopped to invoke a tool (still healthy)
#   - "stop_sequence" : assistant hit a configured stop sequence (healthy)
#   - "max_tokens"    : assistant ran out of output budget (not a hard error,
#                       but downstream callers may want to retry)
#   - "error"         : provider/transport/wrapper error
#   - "aborted"       : run was cancelled (signal, timeout, user kill)
#   - None            : claude did not surface a stop_reason field (older
#                       runtime, or output we could not parse)
#
# success is derived from stop_reason in {"end_turn", "tool_use",
# "stop_sequence"}. It is forced False when stop_reason is "error" or
# "aborted", regardless of the legacy `subtype` heuristic. When stop_reason
# is absent or "max_tokens", success falls back to the legacy subtype check
# so already-deployed agents keep their existing behaviour.
STOP_REASON_HEALTHY = frozenset({"end_turn", "tool_use", "stop_sequence"})
STOP_REASON_FAIL = frozenset({"error", "aborted"})


# ---------------------------------------------------------------------------
# Provider-error envelope detection
#
# Claude Code's ``-p`` mode sometimes returns ``subtype=success`` and a
# healthy ``stop_reason`` even when the underlying API call hit a
# rate-limit, an auth failure, an Anthropic 529 overload, or a usage cap.
# The error body leaks into ``result_text`` and (usually) ``is_error=true``
# is also set. We detect the four common shapes so spend tracking, retry,
# and fleet-wide global block work correctly.
#
# Tightened heuristic (Codex P1 lessons from upstream alfred):
#  * ``is_error=true`` is the primary trigger. When the API explicitly
#    flags the response as an error envelope, we trust it; the regex is
#    just a sanity boost.
#  * Without ``is_error=true``, we require the strict regex match against
#    the JSON error envelope shape. Bare prose mentioning "HTTP 500" or
#    "service unavailable" no longer flips a healthy stop_reason.
#  * Auth + budget regexes match CLI-specific phrasing ("Please run
#    /login", "out of extra usage") that is tight enough to scan against
#    result_text without false-positiving on engineering prose.
#  * Rate-limit detection is the loose one: ``\brate-limit\b`` matches
#    common implementation prose like "added rate-limit handling". Its
#    haystack drops result_text when ``is_error=false`` so a healthy PR
#    summary cannot get reclassified.
# ---------------------------------------------------------------------------
_OVERLOAD_RESULT_RE = re.compile(
    # Anthropic JSON error envelope.
    r'"type"\s*:\s*"error"[^\n]{0,400}?"type"\s*:\s*"overloaded_error"'
    # Literal "API Error" CLI prefix paired with overloaded_error on the
    # same line.
    r"|(?m:^API Error[^\n]{0,400}overloaded_error)"
    # Anthropic 529 explicitly.
    r"|\bHTTP\s*529\b"
    r"|\b529\b\s*[:.\-]\s*(?:overloaded|too\s+many\s+requests)"
    # Bedrock throttle inside an error envelope (not bare prose).
    r'|"type"\s*:\s*"error"[^\n]{0,400}?[Bb]edrock[^\n]{0,400}?throttl(?:ing|ed)'
    r'|"type"\s*:\s*"error"[^\n]{0,400}?throttl(?:ing|ed)[^\n]{0,400}?[Bb]edrock',
    re.IGNORECASE,
)

_AUTH_RESULT_RE = re.compile(
    r"authentication_(?:error|failed)|failed to authenticate|invalid authentication credentials"
    r"|\bAPI Error:\s*401\b|\b401\b[^\n]{0,120}authentication"
    r"|not logged in|please run /login",
    re.IGNORECASE,
)

_BUDGET_RESULT_RE = re.compile(
    r"\b(?:you(?:'re| are) out of extra usage|you(?:'ve| have) hit your usage limit)\b"
    r"|\bout of extra usage\b",
    re.IGNORECASE,
)

_RATE_LIMIT_RESULT_RE = re.compile(
    r"\brate[_ -]?limit(?:ed|_exceeded| exceeded)?\b"
    r"|\b429\b|\btoo many requests\b|\bquota exceeded\b",
    re.IGNORECASE,
)


def _truthy_env(name: str) -> bool:
    """Standard ``1 / true / yes / on`` env-truthiness check."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _claude_credentials_file() -> Path:
    """Return Claude Code's legacy disk credential cache path.

    Current Claude Code on macOS uses Keychain as the live credential store,
    but older or stale ``.credentials.json`` files can still be picked up
    by non-interactive subprocesses and produce a 401 despite ``claude
    auth status`` reporting logged in. We never delete the file; we
    quarantine it and let the CLI fall back to Keychain on the retry.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if config_dir:
        return Path(config_dir).expanduser() / ".credentials.json"
    return HOME / ".claude" / ".credentials.json"


def _quarantine_stale_claude_credentials(reason: str) -> bool:
    """Move a stale Claude credential cache out of the way, if present.

    Disabled by setting ``ALFRED_DISABLE_CLAUDE_AUTH_REPAIR=1``. Returns
    True only when a file was moved and a retry is worth attempting.
    """
    if _truthy_env("ALFRED_DISABLE_CLAUDE_AUTH_REPAIR"):
        return False
    path = _claude_credentials_file()
    if not path.exists():
        return False
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak.auth-failed-{stamp}")
    try:
        path.replace(target)
    except OSError as exc:
        print(
            f"[claude-auth-repair] could not quarantine {path}: {exc}",
            file=sys.stderr,
        )
        return False
    print(
        f"[claude-auth-repair] quarantined stale credential cache {path} -> {target} "
        f"after {reason}; retrying once",
        file=sys.stderr,
    )
    return True


@dataclass
class ClaudeResult:
    success: bool
    subtype: str  # "success" | "error_max_turns" | "error_budget" | "error_rate_limit" | etc.
    num_turns: int
    cost_usd: float
    session_id: str | None
    result_text: str
    raw: dict
    # New (additive) fields - opt-in for callers. Existing agents that read
    # only the five legacy fields keep working unchanged.
    stop_reason: str | None = None
    error_message: str | None = None


def _derive_success(subtype: str, stop_reason: str | None) -> bool:
    """Single source of truth for the success boolean.

    stop_reason wins when it carries a definite signal. We fall back to the
    legacy subtype heuristic only when stop_reason is None or "max_tokens"
    so that callers see the same answer they did before this PR for those
    cases."""
    if stop_reason in STOP_REASON_FAIL:
        return False
    if stop_reason in STOP_REASON_HEALTHY:
        return True
    # stop_reason is None or "max_tokens" or some new value we don't model
    # yet - fall back to the legacy heuristic for backward compat.
    return subtype == "success"


def _build_claude_result(raw: dict, *, fallback_text: str = "") -> ClaudeResult:
    """Build a ClaudeResult from the parsed final JSON event.

    Centralises the stop_reason -> success mapping plus envelope-shape
    error reclassification so tests hit the same code path the runtime
    hits.

    Trigger discipline:
      * ``is_error=true`` is the primary reclassification trigger. When
        the API explicitly flags the response as an error envelope, we
        trust it and the regex serves only as corroboration.
      * Without ``is_error=true`` we require a strict regex match against
        a JSON error envelope shape — bare prose mentioning "throttling"
        no longer flips a healthy stop_reason.
      * Rate-limit detection (``\\brate-limit\\b``) is the loose one and
        false-positives on engineering prose like "added rate-limit
        handling". result_text is mixed into its haystack only when
        is_error=true. Auth + budget regexes scan the full haystack —
        their patterns are tight enough that implementation prose does
        not collide with them.
    """
    subtype = raw.get("subtype", "missing")
    stop_reason = raw.get("stop_reason")
    if stop_reason is not None and not isinstance(stop_reason, str):
        stop_reason = str(stop_reason)

    result_text = raw.get("result", "") or ""

    strict_haystack = "\n".join(
        str(raw.get(key) or "")
        for key in ("error", "error_message", "errorMessage", "api_error_status")
    )
    is_error_flag = bool(raw.get("is_error"))
    full_haystack = f"{result_text}\n{strict_haystack}"
    looks_auth = bool(_AUTH_RESULT_RE.search(full_haystack))
    looks_budget = bool(_BUDGET_RESULT_RE.search(full_haystack))
    rate_limit_haystack = full_haystack if is_error_flag else strict_haystack
    looks_rate_limit = bool(_RATE_LIMIT_RESULT_RE.search(rate_limit_haystack))
    looks_overloaded = bool(_OVERLOAD_RESULT_RE.search(result_text))

    def mark_error(new_subtype: str) -> None:
        nonlocal subtype, stop_reason
        subtype = new_subtype
        stop_reason = "error"

    if is_error_flag:
        # Primary path: the API said is_error=true. Trust that and pin
        # the subtype specific so auth failures don't masquerade as
        # overloads.
        if stop_reason in STOP_REASON_FAIL:
            pass
        elif looks_budget:
            mark_error("error_budget")
        elif looks_auth:
            mark_error("error_authentication")
        elif looks_overloaded:
            mark_error("error_overloaded")
        elif looks_rate_limit:
            mark_error("error_rate_limit")
        elif stop_reason in STOP_REASON_HEALTHY and str(subtype).startswith("error"):
            # Claude can report e.g. subtype=error_max_turns with
            # stop_reason=tool_use. Preserve the specific subtype while
            # forcing success=False via stop_reason=error.
            mark_error(str(subtype))
        elif stop_reason in STOP_REASON_HEALTHY or (stop_reason is None and subtype == "success"):
            mark_error("error_api")
    elif stop_reason in STOP_REASON_HEALTHY:
        # Defensive path: is_error missing/false but the body carries a
        # genuine provider error marker. The strict regexes make this
        # safe enough for the wrapper edge cases we have seen live.
        if looks_budget:
            mark_error("error_budget")
        elif looks_auth:
            mark_error("error_authentication")
        elif looks_overloaded:
            mark_error("error_overloaded")
        elif looks_rate_limit:
            mark_error("error_rate_limit")

    error_message: str | None = None
    if stop_reason in STOP_REASON_FAIL:
        # Prefer a structured error field if claude provided one; otherwise
        # fall back to the result text or the api_error_status string.
        for key in ("error_message", "errorMessage", "error", "api_error_status"):
            val = raw.get(key)
            if val:
                error_message = str(val)
                break
        if not error_message:
            text = result_text or fallback_text
            error_message = (
                text or f"claude stop_reason={stop_reason}"
            ).strip() or f"claude stop_reason={stop_reason}"

    return ClaudeResult(
        success=_derive_success(subtype, stop_reason),
        subtype=subtype,
        num_turns=int(raw.get("num_turns", 0) or 0),
        cost_usd=float(raw.get("total_cost_usd", 0) or 0),
        session_id=raw.get("session_id"),
        result_text=result_text,
        raw=raw,
        stop_reason=stop_reason,
        error_message=error_message,
    )


def _should_retry_claude_auth(result: ClaudeResult, *, already_retried: bool) -> bool:
    """Decide whether to retry once after an authentication failure.

    True only when (a) we have not retried this firing yet AND (b) the
    result classified as ``error_authentication`` AND (c) quarantining
    a stale ``.credentials.json`` actually moved a file out of the way.
    Lets the CLI fall back to Keychain on the retry.
    """
    return (
        not already_retried
        and result.subtype == "error_authentication"
        and _quarantine_stale_claude_credentials(result.error_message or result.result_text)
    )


def claude_invoke(
    prompt: str,
    *,
    workdir: Path,
    allowed_tools: str,
    max_turns: int | None = None,
    timeout: int = 1200,
    resume_session: str | None = None,
    model: str | None = None,
    _auth_retry: bool = False,
) -> ClaudeResult:
    """Invoke `claude -p` with the given prompt. Returns parsed result.

    Uses `--output-format json` (single final event). The returned
    ClaudeResult exposes both the legacy fields (success, subtype,
    result_text, num_turns, cost_usd) and the new stop_reason / error_message
    pair ported from pi-mono. See module-level comment for the discipline.

    ``max_turns`` caps the per-firing turn budget when explicitly provided.
    When None (the default), the wrapper passes ``--max-turns
    _CLAUDE_UNLIMITED_TURNS`` so the Claude CLI never falls back to its
    hidden 40-turn ``-p``-mode default. Per-agent daily turn caps in
    SpendState plus the wall-clock ``timeout`` are the real ceilings; the
    per-firing cap is only useful for debugging or emergency containment.

    ``model`` is an optional alias or full model ID forwarded to
    ``claude --model``. When None (the default), the CLI picks its own
    default model so existing callers see no behavioural change. Use
    ``route_llm`` instead of passing ``model`` directly.

    On a one-time ``error_authentication`` classification we quarantine
    a stale ``~/.claude/.credentials.json`` (if any) and retry once,
    letting the CLI fall back to Keychain. ``_auth_retry`` is the
    re-entry guard — set internally on the retry call so we can never
    loop. Disabled entirely by ``ALFRED_DISABLE_CLAUDE_AUTH_REPAIR=1``.
    """
    effective_max_turns = max_turns if max_turns is not None else _CLAUDE_UNLIMITED_TURNS
    cmd = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--allowedTools",
        allowed_tools,
        "--max-turns",
        str(effective_max_turns),
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
    ]
    if model:
        cmd.extend(["--model", model])
    if resume_session:
        cmd.extend(["--resume", resume_session])

    res = run(cmd, cwd=str(workdir), timeout=timeout, capture=True)

    if not res.stdout:
        fallback = ClaudeResult(
            success=False,
            subtype="parse-failed",
            num_turns=0,
            cost_usd=0.0,
            session_id=None,
            result_text=res.stderr or "",
            raw={},
            stop_reason="error",
            error_message="claude produced no stdout",
        )
        return fallback

    try:
        raw = json.loads(res.stdout)
    except json.JSONDecodeError:
        return ClaudeResult(
            success=False,
            subtype="parse-failed",
            num_turns=0,
            cost_usd=0.0,
            session_id=None,
            result_text=res.stdout or res.stderr or "",
            raw={},
            stop_reason="error",
            error_message="claude output unparseable",
        )

    result = _build_claude_result(raw, fallback_text=res.stderr or "")
    if _should_retry_claude_auth(result, already_retried=_auth_retry):
        return claude_invoke(
            prompt,
            workdir=workdir,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            timeout=timeout,
            resume_session=resume_session,
            model=model,
            _auth_retry=True,
        )
    return result


def claude_invoke_streaming(
    prompt: str,
    *,
    workdir: Path,
    allowed_tools: str,
    agent: str,
    firing_id: str,
    max_turns: int | None = None,
    timeout: int = 1200,
    resume_session: str | None = None,
    model: str | None = None,
) -> ClaudeResult:
    """Streaming counterpart of :func:`claude_invoke`. Same return shape.

    The full implementation in the alfred reference fleet pipes
    ``--output-format stream-json`` through a parser that writes a
    per-firing JSONL transcript to
    ``${HERMES_HOME}/state/transcripts/<agent>/<YYYY-MM>/<firing_id>.jsonl``
    so post-hoc tool / skill aggregation works in downstream fleet CLIs.

    The OSS framework currently delegates to plain :func:`claude_invoke`
    for simplicity. Behaviour: identical ``ClaudeResult`` (turns, cost,
    session_id, result_text). Side effect: no transcript file produced.

    The ``agent`` and ``firing_id`` keyword arguments are accepted (so
    callers don't have to change when streaming lands) but currently
    unused. ``max_turns=None`` is mapped to ``_CLAUDE_UNLIMITED_TURNS``
    so the wall-clock ``timeout`` remains the only ceiling — passing a
    finite integer is the operator's emergency / debug knob. The hidden
    40-turn ``-p``-mode default is never reached because we always emit
    ``--max-turns`` explicitly from ``claude_invoke``.
    """
    if max_turns is None:
        max_turns = _CLAUDE_UNLIMITED_TURNS
    return claude_invoke(
        prompt,
        workdir=workdir,
        allowed_tools=allowed_tools,
        max_turns=max_turns,
        timeout=timeout,
        resume_session=resume_session,
        model=model,
    )


def transcript_path(agent: str, firing_id: str) -> Path:
    """Resolve the transcript file path for an (agent, firing_id) pair.

    Convention: ``${HERMES_HOME}/state/transcripts/<agent>/<YYYY-MM>/<firing_id>.jsonl``.
    Currently no transcripts are written (see :func:`claude_invoke_streaming`),
    but the path resolver ships now so consumer agents and downstream log
    viewers don't need to change when streaming lands.
    """
    month = datetime.now(UTC).strftime("%Y-%m")
    return TRANSCRIPTS_ROOT / agent / month / f"{firing_id}.jsonl"


def codex_artifact_paths(agent: str, firing_id: str) -> dict[str, Path]:
    """Canonical artifact paths for a non-interactive Codex run."""
    month = datetime.now(UTC).strftime("%Y-%m")
    directory = CODEX_TRANSCRIPTS_ROOT / agent / month
    directory.mkdir(parents=True, exist_ok=True)
    return {
        "last_message": directory / f"{firing_id}.last.md",
        "stdout": directory / f"{firing_id}.stdout.txt",
        "stderr": directory / f"{firing_id}.stderr.txt",
    }


def _extract_codex_session_id(text: str) -> str | None:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("session id:"):
            return stripped.split(":", 1)[1].strip() or None
    return None


def _extract_codex_tokens(text: str) -> int:
    lines = [line.strip() for line in (text or "").splitlines()]
    for idx, line in enumerate(lines):
        if line == "tokens used" and idx + 1 < len(lines):
            raw = lines[idx + 1].replace(",", "")
            if raw.isdigit():
                return int(raw)
    return 0


def codex_invoke(
    prompt: str,
    *,
    workdir: Path,
    agent: str,
    firing_id: str | None = None,
    timeout: int = 1200,
    model: str | None = None,
    sandbox: str | None = None,
    approval_policy: str | None = None,
    add_dirs: list[Path] | None = None,
    allowed_tools: str | None = None,
    max_turns: int | None = None,
    resume_session: str | None = None,
) -> ClaudeResult:
    """Invoke ``codex exec`` non-interactively and return a ClaudeResult shape.

    Codex does not expose Claude's tool allow-list, max-turn, or resume-session
    semantics. The wrapper rejects those kwargs instead of implying they were
    enforced. Default posture is review-safe: read-only sandbox and no approval
    prompts.
    """
    unsupported = {
        "allowed_tools": allowed_tools,
        "max_turns": max_turns,
        "resume_session": resume_session,
    }
    rejected = [name for name, value in unsupported.items() if value is not None]
    if rejected:
        return ClaudeResult(
            success=False,
            subtype="error",
            num_turns=0,
            cost_usd=0.0,
            session_id=None,
            result_text="",
            raw={},
            stop_reason="error",
            error_message=(
                "codex engine does not support kwargs: "
                + ", ".join(rejected)
                + ". Use sandbox/approval controls, or route this prompt to Claude."
            ),
        )

    if firing_id is None:
        import secrets

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        firing_id = f"{stamp}-{secrets.token_hex(2)}"

    paths = codex_artifact_paths(agent, firing_id)
    cmd = [
        CODEX_BIN,
        "exec",
        "--skip-git-repo-check",
        "--cd",
        str(workdir),
        "--sandbox",
        sandbox or CODEX_DEFAULT_SANDBOX,
        "-c",
        f'approval_policy="{approval_policy or CODEX_APPROVAL_POLICY}"',
        "--output-last-message",
        str(paths["last_message"]),
    ]
    chosen_model = model or CODEX_DEFAULT_MODEL
    if chosen_model:
        cmd.extend(["--model", chosen_model])
    for directory in add_dirs or []:
        cmd.extend(["--add-dir", str(directory)])
    cmd.append("-")

    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            cwd=str(workdir),
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        return ClaudeResult(
            success=False,
            subtype="parse-failed",
            num_turns=0,
            cost_usd=0.0,
            session_id=None,
            result_text=str(e),
            raw={},
            stop_reason="error",
            error_message=f"codex CLI not found: {e}",
        )
    except subprocess.TimeoutExpired as e:
        return ClaudeResult(
            success=False,
            subtype="error_timeout",
            num_turns=0,
            cost_usd=0.0,
            session_id=None,
            result_text=str(e.stdout or ""),
            raw={},
            stop_reason="error",
            error_message=f"codex_invoke exceeded {timeout}s",
        )

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    with contextlib.suppress(OSError):
        paths["stdout"].write_text(stdout)
        paths["stderr"].write_text(stderr)

    try:
        result_text = paths["last_message"].read_text().strip()
    except OSError:
        result_text = ""
    if not result_text:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        result_text = lines[-1] if lines else ""

    combined = f"{stdout}\n{stderr}"
    raw = {
        "engine": "codex",
        "returncode": proc.returncode,
        "stdout_path": str(paths["stdout"]),
        "stderr_path": str(paths["stderr"]),
        "last_message_path": str(paths["last_message"]),
        "tokens_used": _extract_codex_tokens(combined),
        "model": chosen_model,
        "sandbox": sandbox or CODEX_DEFAULT_SANDBOX,
    }
    session_id = _extract_codex_session_id(combined)
    if proc.returncode != 0:
        tail = (stderr or stdout or "").strip()[-1000:]
        subtype = "error_rate_limit" if "usage limit" in tail.lower() else "error"
        return ClaudeResult(
            success=False,
            subtype=subtype,
            num_turns=1,
            cost_usd=0.0,
            session_id=session_id,
            result_text=result_text or tail,
            raw=raw,
            stop_reason="error",
            error_message=tail or f"codex exited {proc.returncode}",
        )
    if not result_text:
        return ClaudeResult(
            success=False,
            subtype="parse-failed",
            num_turns=1,
            cost_usd=0.0,
            session_id=session_id,
            result_text=stderr or stdout,
            raw=raw,
            stop_reason="error",
            error_message="codex produced no final message",
        )

    return ClaudeResult(
        success=True,
        subtype="success",
        num_turns=1,
        cost_usd=0.0,
        session_id=session_id,
        result_text=result_text,
        raw=raw,
        stop_reason="end_turn",
        error_message=None,
    )


def codex_sandbox_for_agent(
    agent: str,
    *,
    default: str = "read-only",
    environ: dict[str, str] | None = None,
) -> str:
    """Resolve the Codex sandbox for an agent.

    Precedence:
    1. ``ALFRED_<AGENT>_CODEX_SANDBOX``
    2. ``<AGENT>_CODEX_SANDBOX`` legacy alias
    3. ``ALFRED_<AGENT>_CODEX_WRITE=1`` -> ``workspace-write``
    4. caller default
    """
    env = environ if environ is not None else os.environ
    slug = _agent_env_slug(agent)
    explicit = (
        env.get(f"ALFRED_{slug}_CODEX_SANDBOX") or env.get(f"{slug}_CODEX_SANDBOX") or ""
    ).strip()
    if explicit:
        return explicit
    if (env.get(f"ALFRED_{slug}_CODEX_WRITE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return "workspace-write"
    return default


def invoke_agent_engine(
    prompt: str,
    *,
    engine: str,
    agent: str,
    firing_id: str,
    workdir: Path,
    claude_allowed_tools: str,
    timeout: int,
    claude_max_turns: int | None = None,
    claude_model: str | None = None,
    codex_timeout: int | None = None,
    codex_model: str | None = None,
    codex_sandbox: str | None = None,
    codex_add_dirs: list[Path] | None = None,
    codex_approval_policy: str | None = None,
    claude_fn: Callable[..., ClaudeResult] | None = None,
    codex_fn: Callable[..., ClaudeResult] | None = None,
    on_fallback: Callable[[ClaudeResult], None] | None = None,
) -> tuple[ClaudeResult, str]:
    """Invoke a prompt through Claude, Codex, or Claude-first hybrid."""
    mode = normalize_engine(engine)
    claude_call = claude_fn or claude_invoke_streaming
    codex_call = codex_fn or codex_invoke

    def _invoke_claude() -> ClaudeResult:
        return claude_call(
            prompt,
            workdir=workdir,
            allowed_tools=claude_allowed_tools,
            agent=agent,
            firing_id=firing_id,
            max_turns=claude_max_turns,
            timeout=timeout,
            model=claude_model,
        )

    def _invoke_codex() -> ClaudeResult:
        return codex_call(
            prompt,
            workdir=workdir,
            agent=agent,
            firing_id=firing_id,
            timeout=codex_timeout or timeout,
            model=codex_model,
            sandbox=codex_sandbox,
            approval_policy=codex_approval_policy,
            add_dirs=codex_add_dirs,
        )

    if mode == "codex":
        return _invoke_codex(), "codex"

    result = _invoke_claude()
    if mode == "hybrid" and result.subtype in PROVIDER_LIMIT_SUBTYPES:
        if on_fallback:
            on_fallback(result)
        return _invoke_codex(), "codex-fallback"
    return result, "claude"


# ---------- gh CLI helpers ----------


def gh_pr_create(
    repo_slug: str,
    *,
    title: str,
    body_file: Path,
    head: str | None = None,
    labels: list[str] | None = None,
    base: str = "main",
) -> str | None:
    """Open a PR. Pre-ensures labels exist. Returns PR URL or None on failure.

    Logs the gh stderr to ``stderr`` on failure: the prior
    silent-None-return pattern made PR-open failures opaque. The
    runner saw "PR open failed" with no clue whether the cause was
    push, label, branch protection, or anything else; the worktree
    then got cleaned up and forensics were lost. Now the gh error
    message reaches the firing's stderr / Slack alert path.

    Also opportunistically creates any ad-hoc labels not in
    ``STANDARD_LABELS`` with a neutral grey colour. Belt-and-braces:
    a future caller passing a brand-new label without first
    extending STANDARD_LABELS still gets a working PR.
    """
    if labels:
        ensure_labels(repo_slug)
        # Ad-hoc labels (anything passed in but not in STANDARD_LABELS)
        # get created on the fly so a future caller passing a new
        # label without updating STANDARD_LABELS still ships.
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
    # Last line is the URL.
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

    Runner-side mirror of the prompt's Step 1.5 dedup. We search any
    open PR whose title or body mentions ``#<issue_num>`` (this is how
    Conventional-Commits ``Closes #N`` / ``Fixes #N`` link ends up in
    the body) and only consider PRs carrying ``label`` so a human PR
    that happens to reference the issue does NOT lock the queue. We
    skip the issue if the agent is going to step on its own toes; we
    do NOT block the queue on third-party PRs.

    Returns the PR JSON dict (with ``number``, ``url``, ``state``,
    ``labels``, ``title``, ``body``) or ``None`` if no such PR exists.
    Falls back to ``None`` on any ``gh`` failure so a transient
    network blip does not lock out the picker.
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
        # Be defensive: ``gh``'s text search substring-matches, so a
        # PR mentioning ``#12345`` matches a search for ``#12``. Re-
        # validate the body+title contain the exact issue token
        # followed by a non-digit (or end-of-text) so we never lock
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


# ---------- Issue claim state machine ----------
#
# Cooperative cross-actor coordination via GitHub labels + structured
# comments. Designed to prevent duplicate work between any pair of
# (agent, agent) or (agent, operator) actors who might both want to act
# on the same issue.
#
# Race-resistant in the cooperative case (host launchctl already
# serializes per-codename firings via with_lock) and auditable in the
# rare contested case via the claim/release comment trail.
#
# Lifecycle labels (mutually exclusive — at most one at a time per issue):
#   agent:in-flight   - Some agent is actively working it (claim_issue sets)
#   agent:pr-open     - A PR exists (release_issue transitions to)
#   agent:done        - Closed/shipped (set externally on merge)
#
# Sticky modifiers (orthogonal):
#   do-not-pickup     - Operator override; agents skip this issue
#   needs:human-scope - Escalated; not eligible for autonomous pickup
#
# Claim comments — HTML comments, machine-parseable, posted alongside
# the label change so the audit log survives even if the label is later
# stripped or replaced manually:
#   <!-- agent-claim:codename=<name> firing_id=<id> ts=<iso8601> -->
#   <!-- agent-release:codename=<name> firing_id=<id> outcome=<str> ts=<iso8601> -->
#
# Stale-claim sweep: a separate cleanup runner reads claim comments and
# force-releases any in-flight claim that has no matching release after
# a configurable age. This is the belt-and-suspenders that recovers from
# a runner crashing between claim and release.

CLAIM_COMMENT_PREFIX = "<!-- agent-claim:"
RELEASE_COMMENT_PREFIX = "<!-- agent-release:"
PAUSED_REPOS_FILE = STATE_ROOT / "paused-repos.json"

# Framework-provided labels for the state machine. claim_issue ensures
# these exist on the target repo on first call. Operators don't need to
# extend STANDARD_LABELS for the lifecycle to work — it's self-contained.
LIFECYCLE_LABELS: list[tuple[str, str, str]] = [
    (
        "agent:in-flight",
        "e11d21",
        "An agent is actively working this issue. Set before worktree, cleared on exit.",
    ),
    ("agent:pr-open", "fbca04", "A PR exists for this issue. Set by release_issue on success."),
    ("agent:done", "0e8a16", "Issue shipped. Set externally on PR merge."),
    ("do-not-pickup", "5319e7", "Operator override: agents must not claim this issue."),
    (
        "needs:human-scope",
        "e99695",
        "Issue requires manual scoping; not eligible for autonomous pickup.",
    ),
]


def is_repo_paused(repo_slug: str) -> bool:
    """Operator-override: is this repo currently paused?

    Reads ``${HERMES_HOME}/state/paused-repos.json`` (JSON of shape
    ``{"paused": ["repo-slug", ...]}``). Missing or unparseable file is
    treated as "no repos paused" (fail-open). Pausing a repo causes every
    consumer's pick_* helper to skip it without disturbing cross-repo work.
    """
    if not PAUSED_REPOS_FILE.exists():
        return False
    try:
        data = json.loads(PAUSED_REPOS_FILE.read_text())
    except (json.JSONDecodeError, ValueError, OSError):
        return False
    return repo_slug in (data.get("paused", []) or [])


def list_paused_repos() -> list[str]:
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


# ---------------------------------------------------------------------------
# Fleet-wide agent enable/disable
#
# Runner-level gate for opt-in agents. State lives at
# ``$HERMES_HOME/state/fleet/enabled.txt`` — newline-separated codenames,
# ``#`` comments allowed. Operators edit this with vi at 2am, so a flat
# text file beats JSON for survival under "I just want to add one line".
#
# Missing file or missing codename returns ``default``. Callers pick:
# True for default-enabled agents, False for opt-in agents like a brand-new
# feature being burned in. Listed codenames are always enabled.
# ---------------------------------------------------------------------------
FLEET_DIR = STATE_ROOT / "fleet"
FLEET_ENABLED_FILE = FLEET_DIR / "enabled.txt"


def _read_enabled_codenames() -> list[str]:
    """Parse ``FLEET_ENABLED_FILE`` into the list of enabled codenames.

    Skips blank lines and ``#``-prefixed comments. Inline comments are
    also stripped (``batman # MVP burn-in``). Returns ``[]`` when the
    file is missing or unreadable — callers decide the default-enabled
    behaviour via ``is_agent_enabled``'s ``default`` keyword.
    """
    if not FLEET_ENABLED_FILE.exists():
        return []
    try:
        text = FLEET_ENABLED_FILE.read_text()
    except OSError:
        return []
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    seen: set[str] = set()
    deduped: list[str] = []
    for c in out:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def is_agent_enabled(codename: str, *, default: bool = True) -> bool:
    """Return True iff ``codename`` is enabled via the fleet state file.

    File missing → ``default`` (True for opt-out agents, False for
    opt-in agents like Batman until burn-in).
    File present and codename listed → True.
    File present and codename not listed → ``default``.
    """
    if not FLEET_ENABLED_FILE.exists():
        return default
    return codename in _read_enabled_codenames() or default


def list_enabled_agents() -> list[str]:
    """Return the parsed list of codenames in ``FLEET_ENABLED_FILE``.

    Empty list when the file is missing — callers that want
    ``default-enabled`` semantics should consult ``is_agent_enabled``
    per codename.
    """
    return _read_enabled_codenames()


def _atomic_write(path: Path, text: str) -> None:
    """tmp+rename atomic write. Leaves no half-written file on crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text)
        tmp.replace(path)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def _write_enabled_codenames(codenames: list[str]) -> None:
    """Persist a list of codenames to ``FLEET_ENABLED_FILE`` atomically.
    Sorts for stable diffs and dedupes silently."""
    deduped = sorted({c.strip() for c in codenames if c and c.strip()})
    header = (
        "# Fleet enable list — managed by `alfred enable/disable <agent>`.\n"
        "# One codename per line. Blank lines and `#`-comments are ignored.\n"
        "# Edit by hand at your own risk; the CLI is the supported path.\n"
    )
    body = "\n".join(deduped)
    _atomic_write(FLEET_ENABLED_FILE, header + body + ("\n" if body else ""))


def enable_agent(codename: str) -> list[str]:
    """Add ``codename`` to ``FLEET_ENABLED_FILE``. Idempotent. Returns
    the new sorted list of enabled codenames."""
    codename = codename.strip()
    if not codename:
        raise ValueError("enable_agent: codename must be non-empty")
    current = set(_read_enabled_codenames())
    current.add(codename)
    out = sorted(current)
    _write_enabled_codenames(out)
    return out


def disable_agent(codename: str) -> list[str]:
    """Remove ``codename`` from ``FLEET_ENABLED_FILE``. Idempotent.
    Returns the new sorted list of enabled codenames."""
    codename = codename.strip()
    if not codename:
        raise ValueError("disable_agent: codename must be non-empty")
    current = set(_read_enabled_codenames())
    current.discard(codename)
    out = sorted(current)
    _write_enabled_codenames(out)
    return out


def _parse_claim_comment(body: str) -> dict:
    """Parse 'codename=X firing_id=Y outcome=Z ts=W' from a claim/release comment body."""
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
    """One-shot fetch of labels + comments + state, used by claim/release/sweep."""
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
    """Atomic-ish claim. Returns True if the claim succeeded, False if blocked.

    Refusal reasons (returns False):
      - The repo is paused via ``set_repo_paused``.
      - The issue is closed.
      - The issue carries any of: ``agent:in-flight``, ``agent:pr-open``,
        ``do-not-pickup``, ``needs:human-scope``.
      - Race: another claim comment with an earlier ``createdAt`` exists
        with no matching release comment. We back out cleanly (remove our
        own claim, post a ``race-yielded`` release comment) so the
        earlier claimant keeps the issue.

    Side effects on success:
      - Removes ``agent:implement`` label.
      - Adds ``agent:in-flight`` label.
      - Posts a structured claim comment with ``codename`` and
        ``firing_id`` for the audit trail.
    """
    if is_repo_paused(repo_slug):
        return False
    state = _issue_state(repo_slug, num)
    if state.get("state") != "OPEN":
        return False
    labels = {lbl["name"] for lbl in state.get("labels", [])}
    blockers = labels & {"agent:in-flight", "agent:pr-open", "do-not-pickup", "needs:human-scope"}
    if blockers:
        return False
    # First-call setup: make sure the lifecycle labels exist on this repo.
    # ensure_labels is process-cached, so the second+ calls are a no-op.
    ensure_labels(repo_slug, LIFECYCLE_LABELS)
    if not gh_issue_edit(
        repo_slug, num, add_labels=["agent:in-flight"], remove_labels=["agent:implement"]
    ):
        return False
    claim_body = (
        f"{CLAIM_COMMENT_PREFIX}codename={codename} firing_id={firing_id} ts={now_iso()} -->"
    )
    if not gh_issue_comment(repo_slug, num, claim_body):
        gh_issue_edit(
            repo_slug, num, add_labels=["agent:implement"], remove_labels=["agent:in-flight"]
        )
        return False
    contested_by = _detect_contested_claim(
        repo_slug,
        num,
        codename=codename,
        firing_id=firing_id,
    )
    if contested_by is not None:
        gh_issue_edit(
            repo_slug, num, add_labels=["agent:implement"], remove_labels=["agent:in-flight"]
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
    """Release a claim. Optionally transition to a follow-up state label.

    Args:
        outcome: free-form string recorded in the release comment for the
            audit trail. Conventional values: ``success``, ``failure``,
            ``partial``, ``no-commit``, ``rate-limit``, ``max-turns``,
            ``already-implemented``, ``race-yielded``, ``stale-swept``.
        transition_to: optional successor label, e.g. ``agent:pr-open``,
            ``agent:done``. ``None`` returns the issue to the
            ``agent:implement`` queue so it can be re-picked.
        pr_url: optional URL recorded in the release comment for traceability.
    """
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
    """Return the contesting claimant's ``"codename:firing_id"`` if we lost a
    race, else None. Reads recent comments and finds any unreleased claim
    whose ``createdAt`` is earlier than ours.
    """
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
    """List in-flight issues whose latest unreleased claim is older than
    ``max_age_hours``. Returns dicts with number / title / codename /
    firing_id / age_hours. The caller decides whether to force-release.
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
        key = (latest_claim_meta.get("codename"), latest_claim_meta.get("firing_id"))
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
    ``(codename, firing_id)`` so future claim detection can pair the release
    with the original claim. ``sweep_id`` remains in metadata for audit.
    """
    edited = gh_issue_edit(
        repo_slug, num, add_labels=["agent:implement"], remove_labels=["agent:in-flight"]
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
    """Return a structured dedup status for an issue. Used by operator-side
    CLI helpers and pre-push hooks to decide whether claiming or pushing
    an issue-referencing branch would race an in-flight agent.
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


# ---------- Top-level entry point ----------


def with_lock(name: str):
    """Acquire the per-agent lock, exiting if another live PID holds it."""
    lock = AgentLock(name)
    if not lock.acquire():
        print(f"[{name}-LOCKED] previous run still active. Skipping firing.")
        sys.exit(0)
    import atexit

    atexit.register(lock.release)
    return lock


def pid_start_key(pid: int) -> str:
    try:
        res = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def lock_pid_identity_matches(lock_dir: Path, pid: int) -> bool:
    metadata_file = lock_dir / "metadata.json"
    if not metadata_file.exists():
        return False
    try:
        metadata = json.loads(metadata_file.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if metadata.get("pid") != pid:
        return False
    expected_start = metadata.get("pid_start_key")
    if not expected_start:
        return False
    return pid_start_key(pid) == expected_start


def short(text: str, n: int = 300) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "..."


# ---------- Shared-agent brain helpers (additive, opt-in per agent) ----------
#
# These wrap the ported agentic-stack brain at ~/.hermes/shared/.agent/.
# Existing agents work unchanged; new (or freshly-patched) agents can opt in
# with one line each. See alfred:agents/engineering/SHARED_AGENT_INTEGRATION.md
# for the per-agent integration contract.

SHARED_AGENT = HERMES_HOME / "shared" / ".agent"


def _shared_agent_available() -> bool:
    """Return True iff the brain is mounted and the core modules import.

    Conservative: any failure returns False so a missing or broken brain
    does NOT take down a working agent that opted in. Brain is an
    enhancement, not a load-bearing dependency."""
    if not SHARED_AGENT.exists():
        return False
    try:
        sys.path.insert(0, str(SHARED_AGENT / "tools"))
        sys.path.insert(0, str(SHARED_AGENT / "harness"))
        sys.path.insert(0, str(SHARED_AGENT / "memory"))
        import memory_reflect  # noqa: F401
        import recall  # noqa: F401

        return True
    except Exception:
        return False


def recall_for(intent: str, top_k: int = 3) -> str:
    """Return a formatted block of relevant past lessons for `intent`.

    Drop the return value into the prompt of `claude_invoke()` so the model
    starts with prior knowledge. Returns empty string if the brain is
    unavailable - never raises."""
    if not _shared_agent_available():
        return ""
    try:
        from recall import format_pretty
        from recall import recall as _recall

        result, meta = _recall(intent, top_k=top_k)
        if not result:
            return ""
        return format_pretty(intent, result, meta)
    except Exception as e:
        print(f"[recall_for] swallowed: {e}", file=sys.stderr)
        return ""


def reflect(
    skill: str,
    action: str,
    outcome: str,
    *,
    success: bool = True,
    importance: int = 5,
    note: str = "",
    confidence: float | None = None,
) -> dict | None:
    """Append an episodic entry from inside any agent.

    Use after every meaningful action so the dream cycle has something to
    cluster on. Returns the written entry (dict) or None if the brain is
    unavailable. Never raises."""
    if not _shared_agent_available():
        return None
    try:
        from memory_reflect import reflect as _reflect

        return _reflect(
            skill_name=skill,
            action=action,
            outcome=outcome,
            success=success,
            importance=importance,
            reflection=note,
            confidence=confidence,
        )
    except Exception as e:
        print(f"[reflect] swallowed: {e}", file=sys.stderr)
        return None


def call_with_guardrail(prompt: str, validator, **kwargs):
    """Invoke claude with output validation and reject-and-retry.

    Thin wrapper around ``harness/guardrail.py:with_guardrail`` so agents
    can use the pattern with one import from the shared lib instead of
    having to mount the harness path themselves.

    ``validator`` is either a callable ``(output) -> (bool, reason|None)``
    or a ``Guardrail`` instance. ``**kwargs`` are forwarded to
    ``claude_invoke`` (workdir, allowed_tools, max_turns, ...) plus the
    optional ``max_retries`` int (default 1).

    Returns a ``GuardedResult``: behaves like a ``ClaudeResult`` for
    attribute access and adds ``guardrail_passed`` (False iff every
    attempt was rejected), ``attempts``, and ``rejection_reasons``.

    Returns None if the shared brain is not mounted; caller should fall
    back to plain ``claude_invoke``. We do NOT raise here for the same
    reason ``recall_for`` swallows: a missing brain must not take down a
    working agent.
    """
    if not _shared_agent_available():
        return None
    try:
        from guardrail import with_guardrail

        max_retries = kwargs.pop("max_retries", 1)
        return with_guardrail(
            prompt,
            validator,
            claude_invoke_fn=claude_invoke,
            max_retries=max_retries,
            **kwargs,
        )
    except Exception as e:
        print(f"[call_with_guardrail] swallowed: {e}", file=sys.stderr)
        return None


def assemble_shared_context(intent: str, budget: int = 16000) -> str:
    """Build the six-slot brain context string for `intent`.

    Use this when constructing the system prompt for `claude -p`. The
    returned string already includes PREFERENCES, WORKSPACE, REVIEW_QUEUE,
    DECISIONS, query-relevant LESSONS, query-relevant EPISODES, and PERMS.
    Empty string if brain unavailable."""
    if not _shared_agent_available():
        return ""
    try:
        from context_budget import build_context

        ctx, _used = build_context(intent, budget=budget)
        return ctx
    except Exception as e:
        print(f"[assemble_shared_context] swallowed: {e}", file=sys.stderr)
        return ""


# ---------- Event-stream helpers (additive, opt-in per agent) -----------
#
# Thin wrappers around shared/.agent/harness/event_stream.py so an agent
# can do ``emit("slack_post", agent="lucius", message="x", posted=True)``
# or
#   with emit_firing("lucius") as f:
#       ...do work...
#       f.success(num_turns=12, cost_usd=0.42)
# without mounting the harness path itself. Every helper swallows
# exceptions: a broken stream must NEVER take down a working agent.
#
# See shared/.agent/harness/event_stream.py in the private reference fleet for the
# Event/EventStream contract and the type vocabulary.


def emit(event_type: str, **payload) -> None:
    """Append one event to the shared stream. Best-effort, no-throw.

    Pull-out fields (``agent``, ``tokens_in``, ``tokens_out``,
    ``cost_usd``, ``tags``) are promoted to top-level Event columns;
    everything else becomes the event's payload dict."""
    if not _shared_agent_available():
        return
    try:
        from event_stream import Event, EventStream

        agent = payload.pop("agent", "unknown")
        tokens_in = int(payload.pop("tokens_in", 0) or 0)
        tokens_out = int(payload.pop("tokens_out", 0) or 0)
        cost_usd = float(payload.pop("cost_usd", 0.0) or 0.0)
        tags = list(payload.pop("tags", []) or [])
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        ev = Event(
            ts=ts,
            agent=agent,
            type=event_type,
            payload=dict(payload),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            tags=tags,
        )
        EventStream().append(ev)
    except Exception as e:
        print(f"[emit] swallowed: {e}", file=sys.stderr)


class _FiringContext:
    """Yielded by ``emit_firing``. Captures success/turns/cost for the
    eventual ``firing_end`` event.

    ``success()`` records the happy-path numbers you want stamped on the
    closing event. If the ``with`` block raises, we emit a typed
    ``error`` event AND ``firing_end(success=False)`` with the exception
    name."""

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self._num_turns: int = 0
        self._cost_usd: float = 0.0
        self._extra: dict = {}
        self._success_called: bool = False

    def success(self, *, num_turns: int = 0, cost_usd: float = 0.0, **extra) -> None:
        """Record happy-path numbers for the closing event."""
        self._success_called = True
        self._num_turns = int(num_turns or 0)
        self._cost_usd = float(cost_usd or 0.0)
        self._extra = dict(extra)

    def __enter__(self) -> _FiringContext:
        emit("firing_start", agent=self.agent)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            emit("error", agent=self.agent, error=f"{exc_type.__name__}: {exc}")
            emit(
                "firing_end",
                agent=self.agent,
                success=False,
                num_turns=self._num_turns,
                cost_usd=self._cost_usd,
                reason=exc_type.__name__,
            )
            return  # propagate; do not suppress
        emit(
            "firing_end",
            agent=self.agent,
            success=bool(self._success_called),
            num_turns=self._num_turns,
            cost_usd=self._cost_usd,
            **self._extra,
        )


def emit_firing(agent: str) -> _FiringContext:
    """Context manager that wraps a single agent firing in start/end events.

    Usage::

        with emit_firing("lucius") as f:
            ...do work...
            f.success(num_turns=12, cost_usd=0.42)

    On clean exit: emits ``firing_end`` with ``success=True`` if
    ``f.success()`` was called, else ``success=False``. On exception:
    emits an ``error`` event then ``firing_end(success=False)`` and
    re-raises (we never swallow user code exceptions)."""
    return _FiringContext(agent)


# ---------- Best-of-N helper (additive, opt-in per agent) -----------
#
# Wraps shared/.agent/harness/best_of_n.py:TaskRun so an agent can do::
#
#     run = best_of_n("lucius", n=2)
#     run.work_factory = lambda placement: make_worktree(...)
#     run.run_attempts(invoke_fn)
#     run.preflight_apply()
#     winner = run.pick_winner()
#     if winner is not None:
#         wt, branch = run.promote(winner)
#         # ...push winner...
#
# Returns None (and prints to stderr) if the brain isn't mounted, so
# existing agents that never call this are unaffected. The agent passes
# its own ``work_factory`` after construction; we don't try to guess it
# here because every agent's worktree shape is different (Lucius needs
# repo + issue, Bane needs repo + commit-sha, etc.).


def best_of_n(
    agent: str, n: int = 2, *, task_id: str | None = None, work_factory: Any | None = None
) -> Any | None:
    """Return a configured ``TaskRun`` for ``agent``, or None if the brain
    is unavailable. ``work_factory`` is required before calling
    ``run_attempts``; it can be passed here OR set on the returned object.
    ``task_id`` defaults to a fresh UUID4."""
    if not _shared_agent_available():
        return None
    try:
        from best_of_n import TaskRun, new_task_id  # type: ignore
    except Exception as e:
        print(f"[best_of_n] swallowed import error: {e}", file=sys.stderr)
        return None

    def _placeholder_factory(_placement: int):
        raise RuntimeError(
            "best_of_n: work_factory not set. Pass work_factory=... or "
            "assign run.work_factory before calling run_attempts()."
        )

    return TaskRun(
        task_id=task_id or new_task_id(),
        agent=agent,
        n_attempts=n,
        work_factory=work_factory or _placeholder_factory,
    )


# ---------- LLM tier routing ----------
#
# Issues / tasks declare which model handles them via the llm-tier:<x>
# label. Lucius / Bane / Ra's al Ghul / etc. read the tier when picking
# work and call route_llm(tier, prompt, ...) instead of claude_invoke
# directly.
#
# The task-creating agent declares the tier via a label like
# `llm-tier:opus`. When no label is present, the default tier (typically
# sonnet) is used.
#
# Aliases (opus / sonnet / haiku) are passed straight through to
# `claude --model`. The CLI resolves them to the latest dated model so
# we don't drift on stale IDs every release. The "local" tier is routed
# to Ollama running on the host (qwen2.5:3b-instruct-q4_K_M). The "codex"
# tier is routed to `codex exec` and is best suited to read-only review.
TIER_TO_MODEL = {
    "opus": "opus",
    "sonnet": "sonnet",
    "haiku": "haiku",
    "local": None,  # routed to Ollama, see _ollama_invoke
    "codex": None,  # routed to codex_invoke
}

OLLAMA_HOST = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:3b-instruct-q4_K_M"
OLLAMA_TIMEOUT_SEC = 30


def get_tier_from_labels(labels: list) -> str:
    """Read llm-tier:<x> from a list of GitHub label objects.

    Each label is a dict like ``{"name": "...", "color": "...", ...}``.
    The first matching llm-tier label wins so callers don't have to
    reason about ordering. Defaults to ``"sonnet"`` when no label is
    present."""
    for lbl in labels or []:
        if not isinstance(lbl, dict):
            continue
        name = lbl.get("name", "")
        if name.startswith("llm-tier:"):
            return name.split(":", 1)[1]
    return "sonnet"


def _ollama_health_ok() -> bool:
    """Quick probe: is Ollama serving on localhost?"""
    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/tags")
        with urllib.request.urlopen(req, timeout=2) as r:
            r.read()
        return True
    except Exception:
        return False


def start_ollama_if_needed() -> bool:
    """Start ``ollama serve`` in the background if it isn't already up.

    Returns True if Ollama is reachable after the call (whether we
    started it or it was already running). Returns False if Ollama is
    not installed or the daemon never came up."""
    if _ollama_health_ok():
        return True
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (FileNotFoundError, OSError):
        return False
    # Give the daemon a moment to bind its port. Poll up to ~5s.
    for _ in range(10):
        time.sleep(0.5)
        if _ollama_health_ok():
            return True
    return False


_OLLAMA_HONORED_KWARGS = {"timeout"}
_OLLAMA_UNSUPPORTED_KWARGS = {
    "workdir",
    "allowed_tools",
    "max_turns",
    "resume_session",
    "model",
    "output_format",
}


def _ollama_invoke(prompt: str, **kw) -> ClaudeResult:
    """POST to Ollama /api/generate and return a ClaudeResult-shaped reply.

    Returns a failure ClaudeResult (success=False, stop_reason="error")
    when Ollama is not running or the request errors, so the caller can
    fall back to ``claude_invoke`` without branching on tier.

    Honors ``timeout`` (overrides ``OLLAMA_TIMEOUT_SEC``) so callers that
    set their own deadline are not silently capped. Kwargs that have no
    Ollama analogue (``workdir``, ``allowed_tools``, ``max_turns``,
    ``resume_session``, ``model``, ``output_format``) are rejected up
    front so a caller does not believe a tool gate or session resume was
    enforced when it wasn't. Unknown kwargs are also rejected."""
    timeout = kw.pop("timeout", OLLAMA_TIMEOUT_SEC)
    unsupported = sorted(set(kw.keys()) & _OLLAMA_UNSUPPORTED_KWARGS)
    unknown = sorted(set(kw.keys()) - _OLLAMA_UNSUPPORTED_KWARGS - _OLLAMA_HONORED_KWARGS)
    rejected = unsupported + unknown
    if rejected:
        return ClaudeResult(
            success=False,
            subtype="error",
            num_turns=0,
            cost_usd=0.0,
            session_id=None,
            result_text="",
            raw={},
            stop_reason="error",
            error_message=(
                "ollama tier does not support kwargs: "
                + ", ".join(rejected)
                + ". Drop them or route this prompt to claude (sonnet/haiku/opus) instead."
            ),
        )
    payload = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate",
        data=payload,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8")
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return ClaudeResult(
            success=False,
            subtype="error",
            num_turns=0,
            cost_usd=0.0,
            session_id=None,
            result_text="",
            raw={},
            stop_reason="error",
            error_message=f"ollama not running: {reason}",
        )
    except Exception as e:
        return ClaudeResult(
            success=False,
            subtype="error",
            num_turns=0,
            cost_usd=0.0,
            session_id=None,
            result_text="",
            raw={},
            stop_reason="error",
            error_message=f"ollama request failed: {type(e).__name__}: {e}",
        )

    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        return ClaudeResult(
            success=False,
            subtype="error",
            num_turns=0,
            cost_usd=0.0,
            session_id=None,
            result_text=body,
            raw={},
            stop_reason="error",
            error_message="ollama response unparseable",
        )

    return ClaudeResult(
        success=True,
        subtype="success",
        num_turns=1,
        cost_usd=0.0,
        session_id=None,
        result_text=str(raw.get("response", "")),
        raw=raw,
        stop_reason="end_turn",
        error_message=None,
    )


def route_llm(tier: str, prompt: str, **kw) -> ClaudeResult:
    """Route a prompt to the right model based on tier.

    ``tier`` in {"opus", "sonnet", "haiku", "local", "codex"}. Unknown tiers
    fall back to sonnet so a typo in a label can't take an agent down.
    All extra kwargs are forwarded to the underlying invoker.

    For ``local``, ensures the Ollama daemon is up before dispatching so
    a cold host does not produce a misleading ``ollama not running``
    failure on the first call."""
    if tier == "codex":
        return codex_invoke(prompt, **kw)
    if tier == "local":
        if not start_ollama_if_needed():
            return ClaudeResult(
                success=False,
                subtype="error",
                num_turns=0,
                cost_usd=0.0,
                session_id=None,
                result_text="",
                raw={},
                stop_reason="error",
                error_message=(
                    "ollama daemon could not be started; the local tier is "
                    "unavailable on this host. Install ollama or route the "
                    "prompt to a claude tier (sonnet/haiku/opus)."
                ),
            )
        return _ollama_invoke(prompt, **kw)
    model = TIER_TO_MODEL.get(tier, TIER_TO_MODEL["sonnet"])
    return claude_invoke(prompt, model=model, **kw)
