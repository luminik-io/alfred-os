"""
alfred-os — shared library for cron-driven Claude Code agents.

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

Consumers (e.g. luminik-io/alfred) write a thin ``bin/<codename>.py`` per
agent that imports from this module, declares a ``PreflightSpec``, and calls
``claude_invoke()``. The runner does no LLM-orchestration work itself; all
real work happens inside a CLI subprocess against the operator's configured
Claude Code subscription, optional Codex login, or any wrapper binary they
configure.

Path defaults assume a single macOS host. ``HERMES_HOME`` defaults to
``~/.hermes`` and ``WORKSPACE_ROOT`` to ``~/Workspace``; both are env-var
overridable. ``GH_ORG`` is required for any helper that targets GitHub
(e.g. ``gh_pr_create``); set it once in the launchd plist's
``EnvironmentVariables`` block.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.request
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
#   LUMINIK_WORKSPACE - root of the per-repo product checkouts (every
#                       <repo> in GH_REPO_TO_LOCAL is a child directory).
#                       Defaults to ~/Claude_Workspace.
#   CLAUDE_BIN        - absolute path to the `claude` CLI. Defaults to
#                       whatever is on $PATH; override only if you have a
#                       non-standard install. Set to a fully-qualified path
#                       on hosts without `claude` on PATH (e.g., launchd).
#   CODEX_BIN         - absolute path to the `codex` CLI. Defaults to
#                       whatever is on $PATH. Only used by llm-tier:codex.
# --------------------------------------------------------------------------
HOME = Path(os.path.expanduser("~"))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))

# WORKSPACE_ROOT is the canonical name. LUMINIK_WORKSPACE is the legacy
# alias preserved for back-compat with the original alfred deployment;
# new consumers should set WORKSPACE_ROOT only.
WORKSPACE_ROOT = Path(
    os.environ.get("WORKSPACE_ROOT")
    or os.environ.get("LUMINIK_WORKSPACE")
    or os.path.expanduser("~/Workspace")
)
LUMINIK_WORKSPACE = WORKSPACE_ROOT  # deprecated alias

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
SLACK_WEBHOOK_CACHE = STATE_ROOT / "slack-webhook.cache"
SLACK_WEBHOOK_CACHE_TTL = 7 * 24 * 3600  # 7 days; the webhook URL itself is stable

# Shared rate-limit blocker — when ANY agent hits Anthropic's error_rate_limit
# or error_budget, all agents respect the block until the timeout passes.
# Otherwise each agent's cron would keep firing into the rate-limit wall.
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
# target repo if missing. Empty default; consumers extend per their workflow.
# Each tuple: (name, hex-color-no-hash, description).
#
#     STANDARD_LABELS.extend([
#         ("agent:authored", "00ff00", "Authored by an autonomous agent"),
#         ("agent:implement", "ffa500", "Picked up by an agent for implementation"),
#     ])
STANDARD_LABELS: list[tuple[str, str, str]] = []


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
    2. Disk cache at ``${HERMES_HOME}/state/slack-webhook.cache`` (7-day TTL)
       — written by step 3 the first time it succeeds, so subsequent calls
       skip the AWS round-trip.
    3. AWS Secrets Manager — secret id from ``SLACK_WEBHOOK_SECRET_ID``
       (default ``slack/agents/webhook-url``), region from
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
        secret_id = os.environ.get("SLACK_WEBHOOK_SECRET_ID", "slack/agents/webhook-url")
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
        sts = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--query", "Arn", "--output", "text"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = (sts.stderr or sts.stdout or "").strip()
        if sts.returncode != 0 or spec.aws_profile not in (sts.stdout or ""):
            err = out.splitlines()[-1] if out else "no output"
            misses.append(f"AWS profile `{spec.aws_profile}` not usable: {err[:120]}")

    # 4. gh auth alive — every issue / PR / label operation needs it.
    if spec.require_gh_auth:
        gh = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=10)
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
    slack_post(f"🚫 {spec.agent} preflight failed: {headline}")
    raise PreflightFailed(misses)


def doctor_mode() -> bool:
    """True when running under doctor.sh (HERMES_DOCTOR=1).

    Agents check this after preflight passes and exit 0 with a [<AGENT>-DOCTOR-OK]
    sentinel instead of doing real work. Lets the operator verify a fresh setup
    without burning Claude turns or making side effects.
    """
    return os.environ.get("HERMES_DOCTOR", "").strip() not in ("", "0", "false", "False")


# ---------- Prompt loading + variable substitution ----------
#
# Codename agents read their system prompt from a markdown file in the repo
# (or from a Hermes cron config). The same file is editable as documentation
# AND consumed by the runner at firing time. To keep operator-specific
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
        (self._lock_dir / "pid").write_text(str(os.getpid()))
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

    Centralises the stop_reason -> success mapping so tests can hit the same
    code path the live runtime hits."""
    subtype = raw.get("subtype", "missing")
    stop_reason = raw.get("stop_reason")
    if stop_reason is not None and not isinstance(stop_reason, str):
        stop_reason = str(stop_reason)

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
            text = raw.get("result") or fallback_text
            error_message = (
                text or f"claude stop_reason={stop_reason}"
            ).strip() or f"claude stop_reason={stop_reason}"

    return ClaudeResult(
        success=_derive_success(subtype, stop_reason),
        subtype=subtype,
        num_turns=int(raw.get("num_turns", 0) or 0),
        cost_usd=float(raw.get("total_cost_usd", 0) or 0),
        session_id=raw.get("session_id"),
        result_text=raw.get("result", "") or "",
        raw=raw,
        stop_reason=stop_reason,
        error_message=error_message,
    )


def claude_invoke(
    prompt: str,
    *,
    workdir: Path,
    allowed_tools: str,
    max_turns: int,
    timeout: int = 1200,
    resume_session: str | None = None,
    model: str | None = None,
) -> ClaudeResult:
    """Invoke `claude -p` with the given prompt. Returns parsed result.

    Uses `--output-format json` (single final event). The returned
    ClaudeResult exposes both the legacy fields (success, subtype,
    result_text, num_turns, cost_usd) and the new stop_reason / error_message
    pair ported from pi-mono. See module-level comment for the discipline.

    ``model`` is an optional alias or full model ID forwarded to
    ``claude --model``. When None (the default), the CLI picks its own
    default model so existing callers see no behavioural change. Use
    ``route_llm`` instead of passing ``model`` directly."""
    cmd = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--allowedTools",
        allowed_tools,
        "--max-turns",
        str(max_turns),
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
        return ClaudeResult(
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

    return _build_claude_result(raw, fallback_text=res.stderr or "")


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
    so post-hoc tool / skill aggregation works (``alfred logs <agent>
    --firing <id>``).

    The OSS framework currently delegates to plain :func:`claude_invoke`
    for simplicity. Behaviour: identical ``ClaudeResult`` (turns, cost,
    session_id, result_text). Side effect: no transcript file produced.

    The ``agent`` and ``firing_id`` keyword arguments are accepted (so
    callers don't have to change when streaming lands) but currently
    unused. ``max_turns=None`` is mapped to a hard ceiling of 200 to
    match the reference fleet's per-firing safety bound.
    """
    if max_turns is None:
        max_turns = 200
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
    but the path resolver ships now so consumer agents and ``alfred logs``
    don't need to change when streaming lands.
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
    """Open a PR. Pre-ensures labels exist. Returns PR URL or None on failure."""
    if labels:
        ensure_labels(repo_slug)
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
        return None
    # Last line is the URL
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
    gh_issue_comment(repo_slug, num, claim_body)
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
    gh_issue_edit(repo_slug, num, add_labels=add, remove_labels=remove)
    pr_part = f" pr={pr_url}" if pr_url else ""
    gh_issue_comment(
        repo_slug,
        num,
        f"{RELEASE_COMMENT_PREFIX}codename={codename} firing_id={firing_id} "
        f"outcome={outcome}{pr_part} ts={now_iso()} -->",
    )
    return True


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


def force_release_stale_claim(repo_slug: str, num: int, *, sweep_id: str) -> bool:
    """Forcibly release a stale claim. Restores ``agent:implement`` so the
    queue picks it up. Records ``outcome=stale-swept`` in the release comment.
    """
    gh_issue_edit(repo_slug, num, add_labels=["agent:implement"], remove_labels=["agent:in-flight"])
    gh_issue_comment(
        repo_slug,
        num,
        f"{RELEASE_COMMENT_PREFIX}codename=cleanup firing_id={sweep_id} "
        f"outcome=stale-swept ts={now_iso()} -->",
    )
    return True


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
    """Context manager-ish helper: acquire lock, exit if held by another live PID."""
    lock = AgentLock(name)
    if not lock.acquire():
        print(f"[{name}-LOCKED] previous run still active. Skipping firing.")
        sys.exit(0)
    import atexit

    atexit.register(lock.release)
    return lock


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
# See infra/agents/shared/.agent/harness/event_stream.py for the
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
