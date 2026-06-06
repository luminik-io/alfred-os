"""Firing-lifecycle orchestrator: preflight, LLM tier routing, opt-in helpers.

This module is the thin coordinator that the per-codename runners in
``bin/`` import. It owns the host-readiness check (:func:`preflight`),
the model-selection seam (:func:`route_llm`), and the additive opt-in
helpers (shared brain, event stream, best-of-N) that degrade silently
when the optional shared-agent directory isn't mounted.

What this module does NOT own:

* Subprocess invocation of ``claude`` / ``codex`` -> ``process.py``.
* Issue claim / release state machine -> ``github.py``.
* On-disk state (locks, spend, fleet flags) -> ``state.py``.
* Slack delivery -> ``notify.py``.

The orchestrator's public surface is re-exported from
``agent_runner.__init__`` so bin scripts can keep importing flat names.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import _env_present, _env_value_enabled
from .disk import disk_pressure_status
from .notify import slack_post
from .paths import SHARED_AGENT, WORKSPACE
from .process import claude_invoke, codex_invoke
from .result import ClaudeResult

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------


@dataclass
class PreflightSpec:
    """Declarative requirements for an agent run.

    All fields are optional: an agent that needs nothing more than
    ``claude`` on PATH and the two workspace env vars can declare just
    ``bins=["claude"]``.
    """

    agent: str
    env_vars: list[str] = field(default_factory=lambda: ["ALFRED_HOME", "WORKSPACE_ROOT"])
    bins: list[str] = field(default_factory=list)
    aws_profile: str | None = None
    require_gh_auth: bool = False
    require_workspace_repos: list[str] = field(default_factory=list)
    # Disk-pressure floors. ``None`` means "use the env-configured /
    # built-in defaults" (``ALFRED_MIN_FREE_DISK_GB`` /
    # ``ALFRED_MIN_FREE_DISK_PCT``), so every agent inherits the guard
    # without opting in. Set explicitly only when an agent needs more
    # headroom than the fleet default (e.g. a build agent that checks out
    # a large monorepo).
    min_free_disk_gb: float | None = None
    min_free_disk_pct: float | None = None
    # When True (the default), a ``critical`` disk-pressure reading makes
    # preflight raise :class:`PreflightFailed` so the firing skips cleanly
    # instead of crash-looping on ENOSPC. Set False only for the cleanup
    # agent itself, which must run *despite* low disk to reclaim space.
    check_disk: bool = True


class PreflightFailed(RuntimeError):
    """Raised by :func:`preflight` when one or more checks fail.

    The caller catches and exits ``0`` cleanly; preflight has already
    posted a one-line Slack message and printed a sentinel to stdout.
    """


def sync_checkout_to_default(
    repo_path: Path,
    *,
    run_cmd: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[bool, str]:
    """Fast-forward a clean default-branch checkout before an agent reads it.

    Dirty checkouts, detached heads, and feature branches are deliberately
    skipped: preflight must never discard operator work.
    """
    if _env_value_enabled("ALFRED_DISABLE_CHECKOUT_SYNC"):
        return True, "sync disabled"

    def git(
        args: list[str],
        *,
        timeout: int,
    ) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
        command = ["git", *args]
        try:
            return (
                run_cmd(
                    command,
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                ),
                None,
            )
        except subprocess.TimeoutExpired:
            return None, f"git {' '.join(args)} timed out after {timeout}s"
        except OSError as exc:
            return None, f"git {' '.join(args)} failed: {exc}"

    status, error = git(["status", "--porcelain"], timeout=15)
    if error:
        return False, error
    assert status is not None
    if status.returncode != 0:
        detail = (status.stderr or status.stdout or "").strip().splitlines()
        return False, f"git status failed: {(detail[-1] if detail else status.returncode)}"
    if (status.stdout or "").strip():
        return True, "skipped: checkout dirty"

    branch, error = git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    if error:
        return False, error
    assert branch is not None
    if branch.returncode != 0:
        return True, "skipped: branch unavailable"
    current_branch = (branch.stdout or "").strip()
    if not current_branch or current_branch == "HEAD":
        return True, "skipped: detached HEAD"

    default_ref, error = git(
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        timeout=10,
    )
    if error:
        return False, error
    assert default_ref is not None
    default_branch = "main"
    if default_ref.returncode == 0:
        ref = (default_ref.stdout or "").strip()
        if ref.startswith("origin/"):
            default_branch = ref.split("/", 1)[1]
    if current_branch != default_branch:
        return True, f"skipped: on {current_branch}, default is {default_branch}"

    fetch, error = git(["fetch", "origin", default_branch], timeout=60)
    if error:
        return False, error
    assert fetch is not None
    if fetch.returncode != 0:
        detail = (fetch.stderr or fetch.stdout or "").strip().splitlines()
        return False, f"git fetch failed: {(detail[-1] if detail else fetch.returncode)}"

    merge, error = git(["merge", "--ff-only", f"origin/{default_branch}"], timeout=60)
    if error:
        return False, error
    assert merge is not None
    if merge.returncode != 0:
        detail = (merge.stderr or merge.stdout or "").strip().splitlines()
        return False, f"git merge --ff-only failed: {(detail[-1] if detail else merge.returncode)}"

    ahead, error = git(["rev-list", "--count", f"origin/{default_branch}..HEAD"], timeout=10)
    if error:
        return False, error
    assert ahead is not None
    if ahead.returncode != 0:
        detail = (ahead.stderr or ahead.stdout or "").strip().splitlines()
        return False, f"git ahead check failed: {(detail[-1] if detail else ahead.returncode)}"
    ahead_count = (ahead.stdout or "").strip()
    if ahead_count and ahead_count != "0":
        return False, f"checkout has {ahead_count} local commit(s) ahead of origin/{default_branch}"
    return True, f"synced origin/{default_branch}"


def preflight(spec: PreflightSpec) -> None:
    """Validate the host before doing real work. Raise on miss.

    Reports every miss in one shot rather than one-at-a-time so the
    operator sees the full picture in a single Slack notification.

    Args:
        spec: declarative requirements for this firing.

    Raises:
        PreflightFailed: when one or more checks fail.
    """
    import shutil  # local: only used when an agent actually checks bins

    # 0. Disk-pressure gate (the ENOSPC crash-loop fix).
    #
    # Runs before the env/bin/AWS/gh checks: if the disk is critically
    # full there is no point validating auth — the firing would only
    # crash on ENOSPC. On a critical reading we fire emergency cleanup
    # once, re-probe, and if still critical raise PreflightFailed so the
    # agent exits 0 cleanly (every runner already catches PreflightFailed
    # and sys.exit(0)). This is what stops the launchd job from looping.
    if spec.check_disk:
        _disk_preflight_gate(spec)

    misses: list[str] = []

    # 1. Required env vars.
    for var in spec.env_vars:
        if not _env_present(var):
            misses.append(f"env var `{var}` is unset")

    # 2. Required CLI binaries on PATH (or absolute paths that are executable).
    for binname in spec.bins:
        if "/" in binname:
            if not Path(binname).is_file() or not os.access(binname, os.X_OK):
                misses.append(f"binary `{binname}` is not an executable file")
        elif not shutil.which(binname):
            misses.append(f"binary `{binname}` not found on PATH")

    # 3. AWS profile usable. Strip inherited keys so we never accidentally
    #    use the operator's interactive SSO session in place of the
    #    agent's scoped IAM user.
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
                [
                    "aws",
                    "sts",
                    "get-caller-identity",
                    "--query",
                    "Arn",
                    "--output",
                    "text",
                ],
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

    # 4. gh auth alive (every issue/PR/label operation needs it).
    if spec.require_gh_auth:
        try:
            gh = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            misses.append("gh auth status timed out")
        except FileNotFoundError:
            misses.append("binary `gh` not found on PATH")
        else:
            if gh.returncode != 0:
                misses.append("gh auth not active (run `gh auth login`)")

    # 5. Local repo checkouts present.
    # Resolve via ``WORKSPACE`` (which honours ``WORKSPACE_SUBDIR``) so a
    # fleet that ran ``alfred-init`` with ``WORKSPACE_SUBDIR=src`` (or empty,
    # for ``$WORKSPACE_ROOT/<repo>`` directly) is checked at the same path
    # the runtime will actually clone into. Hard-coding ``"product"`` here
    # made the new layout override unusable for scheduled runs (preflight
    # rejected every repo even though ``$WORKSPACE_ROOT/src/<repo>/.git``
    # existed).
    #
    # Also consult ``GH_REPO_TO_LOCAL`` (populated by an optional fleet
    # overlay) to map a github-slug to the on-disk directory name, falling
    # back to the slug itself when no mapping is registered.
    try:
        from .github import GH_REPO_TO_LOCAL as _slug_to_local
    except ImportError:
        _slug_to_local = {}
    for repo in spec.require_workspace_repos:
        local = _slug_to_local.get(repo, repo)
        repo_path = WORKSPACE / local
        if not (repo_path / ".git").exists():
            misses.append(f"checkout `{repo_path}` missing or not a git repo")
            continue
        ok, sync_message = sync_checkout_to_default(repo_path)
        if not ok:
            misses.append(f"checkout `{repo_path}` could not sync: {sync_message}")

    if not misses:
        return

    sentinel = f"[{spec.agent.upper()}-PREFLIGHT-FAILED]"
    detail = "\n  ".join(f"- {m}" for m in misses)
    print(f"{sentinel} {len(misses)} issue(s):\n  {detail}")
    headline = misses[0] + (f" (+{len(misses) - 1} more)" if len(misses) > 1 else "")
    suppress_slack = (
        _env_value_enabled("ALFRED_DOCTOR")
        or spec.agent == "test"
        or (
            not _env_value_enabled("XPC_SERVICE_NAME")
            and not _env_value_enabled("ALFRED_PREFLIGHT_FORCE_SLACK")
        )
    )
    if not suppress_slack:
        # Throttle the Slack post to once per N minutes per
        # (agent, error_signature). When an agent's preflight fails
        # identically on every tick (AWS profile rotation, gh auth
        # expiry), the previous code path posted on every firing —
        # 48+ identical posts per day. The operator stops reading
        # the channel and the actual signal is lost. With a throttle,
        # the operator sees one ping per error per hour (configurable
        # via ALFRED_PREFLIGHT_SLACK_MIN_MINUTES).
        signature = _preflight_error_signature(misses)
        if _should_post_preflight_slack(spec.agent, signature):
            slack_post(f"🚫 {spec.agent} preflight failed: {headline}")
            _record_preflight_slack_post(spec.agent, signature)
        else:
            print(
                f"[{spec.agent}-preflight-slack-throttled] same error within "
                f"{_preflight_slack_min_minutes()}m; skipping Slack post.",
                file=sys.stderr,
            )
    raise PreflightFailed(misses)


# --------------------------------------------------------------------------
# Per-(agent, error_signature) Slack throttle for preflight failures.
#
# State file at $ALFRED_HOME/state/<agent>/last-slack-preflight-post.json
# maps error_signature -> ISO timestamp of the most recent Slack post for
# that signature. Default window is 60 minutes, override via
# ALFRED_PREFLIGHT_SLACK_MIN_MINUTES. State-file errors fail-open: a
# corrupt or unreadable state file must never silence preflight
# escalation, only the inverse.
# --------------------------------------------------------------------------

_PREFLIGHT_SLACK_STATE_NAME = "last-slack-preflight-post.json"


def _preflight_slack_min_minutes() -> int:
    try:
        return int(os.environ.get("ALFRED_PREFLIGHT_SLACK_MIN_MINUTES", "60"))
    except ValueError:
        return 60


def _preflight_error_signature(misses: list[str]) -> str:
    """Hash-stable identifier for one preflight's set of misses."""
    return ";".join(sorted(misses))


def _preflight_slack_state_path(agent: str) -> Path:
    from .paths import STATE_ROOT

    return STATE_ROOT / agent / _PREFLIGHT_SLACK_STATE_NAME


def _should_post_preflight_slack(agent: str, signature: str) -> bool:
    state_path = _preflight_slack_state_path(agent)
    if not state_path.exists():
        return True
    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return True
    last_iso = data.get(signature) if isinstance(data, dict) else None
    if not isinstance(last_iso, str):
        return True
    try:
        last_dt = datetime.strptime(last_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return True
    delta_minutes = (datetime.now(UTC) - last_dt).total_seconds() / 60.0
    return delta_minutes >= _preflight_slack_min_minutes()


def _record_preflight_slack_post(agent: str, signature: str) -> None:
    """Stamp the (agent, signature) pair. Best-effort write."""
    state_path = _preflight_slack_state_path(agent)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if state_path.exists():
            try:
                loaded = json.loads(state_path.read_text())
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, json.JSONDecodeError, ValueError):
                data = {}
        data[signature] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        state_path.write_text(json.dumps(data, indent=2, sort_keys=True))
    except OSError as e:
        print(
            f"[{agent}-preflight-slack-state-write-failed] {e}",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------
# Disk-pressure preflight gate (ENOSPC crash-loop fix).
#
# On a ``critical`` reading we run emergency cleanup ONCE (guarded by a
# per-run env flag so a re-probe can't recurse), re-probe, and only then
# decide to skip. Skipping raises PreflightFailed, which every runner
# already catches and turns into sys.exit(0). A throttled Slack warning
# (once per ALFRED_DISK_SLACK_MIN_HOURS, default 6h) tells the operator
# the fleet is paused on disk pressure without spamming the channel on
# every tick.
# --------------------------------------------------------------------------

# Set while emergency cleanup runs so a nested preflight (the cleanup
# agent's own) cannot trigger a second emergency pass — guards the loop.
_DISK_EMERGENCY_GUARD_ENV = "ALFRED_DISK_EMERGENCY_IN_PROGRESS"

_DISK_SLACK_STATE_NAME = "last-slack-disk-warning.json"


def _disk_slack_min_hours() -> int:
    try:
        return max(1, int(os.environ.get("ALFRED_DISK_SLACK_MIN_HOURS", "6")))
    except ValueError:
        return 6


def _disk_slack_state_path(agent: str) -> Path:
    from .paths import STATE_ROOT

    return STATE_ROOT / agent / _DISK_SLACK_STATE_NAME


def _should_post_disk_slack(agent: str) -> bool:
    """True when no disk warning was posted within the throttle window.

    Fails open: any unreadable/corrupt state file means "post", so a
    disk alert is never silently lost.
    """
    state_path = _disk_slack_state_path(agent)
    if not state_path.exists():
        return True
    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return True
    last_iso = data.get("last") if isinstance(data, dict) else None
    if not isinstance(last_iso, str):
        return True
    try:
        last_dt = datetime.strptime(last_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return True
    delta_hours = (datetime.now(UTC) - last_dt).total_seconds() / 3600.0
    return delta_hours >= _disk_slack_min_hours()


def _record_disk_slack_post(agent: str) -> None:
    """Stamp the most-recent disk-warning post time. Best-effort write."""
    state_path = _disk_slack_state_path(agent)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {"last": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
                indent=2,
            )
        )
    except OSError as e:
        print(f"[{agent}-disk-slack-state-write-failed] {e}", file=sys.stderr)


def _run_emergency_cleanup(agent: str) -> None:
    """Invoke ``bin/agent-cleanup.py --emergency`` once to reclaim space.

    Best-effort and bounded: a missing script, a crash, or a timeout must
    never turn a disk-pressure skip into a hard failure — the firing is
    going to skip cleanly regardless. The ``_DISK_EMERGENCY_GUARD_ENV``
    flag is set in the child env so the cleanup agent's own preflight
    cannot trigger a second emergency pass. We deliberately do not set it
    in our own ``os.environ``: the caller's post-cleanup re-probe is a
    plain ``disk_pressure_status()`` read, not a nested preflight, so it
    can't recurse — and leaving our process env untouched avoids leaking
    the guard into the rest of this firing.
    """
    from .paths import ALFRED_HOME

    candidates = [
        ALFRED_HOME / "bin" / "agent-cleanup.py",
        Path(__file__).resolve().parent.parent.parent / "bin" / "agent-cleanup.py",
    ]
    script = next((c for c in candidates if c.exists()), None)
    if script is None:
        print(
            f"[{agent}-disk-emergency] agent-cleanup.py not found; skipping reclaim",
            file=sys.stderr,
        )
        return

    env = dict(os.environ)
    env[_DISK_EMERGENCY_GUARD_ENV] = "1"
    print(f"[{agent}-disk-emergency] running {script} --emergency", file=sys.stderr)
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--emergency"],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[{agent}-disk-emergency] cleanup failed: {e}", file=sys.stderr)
        return
    out = (result.stdout or "").strip()
    if out:
        for line in out.splitlines():
            print(f"[{agent}-disk-emergency] {line}", file=sys.stderr)


def _disk_preflight_gate(spec: PreflightSpec) -> None:
    """Skip the firing cleanly when the disk is critically full.

    Probes the filesystem holding ALFRED_HOME against the spec's
    (or env-configured) floors. On a ``critical`` reading: run emergency
    cleanup once, re-probe, and if still critical post a throttled Slack
    warning and raise :class:`PreflightFailed` so the runner exits 0.

    No-ops (returns) when disk is healthy, when already inside an
    emergency-cleanup pass (loop guard), or when the optional per-spec
    floors leave the env defaults in place and the disk is fine.
    """
    # Loop guard: if we are already inside an emergency cleanup pass, do
    # not probe again — the cleanup agent must be allowed to run.
    if _env_value_enabled(_DISK_EMERGENCY_GUARD_ENV):
        return

    # Apply per-spec floor overrides via env for the duration of the
    # probe so disk_pressure_status reads them. Restore afterwards so we
    # never leak agent-specific floors into the rest of the process.
    overrides = {
        "ALFRED_MIN_FREE_DISK_GB": spec.min_free_disk_gb,
        "ALFRED_MIN_FREE_DISK_PCT": spec.min_free_disk_pct,
    }
    saved = {k: os.environ.get(k) for k in overrides}
    try:
        for key, value in overrides.items():
            if value is not None:
                os.environ[key] = str(value)
        status = disk_pressure_status()
        if not status["critical"]:
            return

        print(
            f"[{spec.agent.upper()}-DISK-CRITICAL] "
            f"free={status['free_gb']:.1f}GB ({status['free_pct']:.1f}%); "
            "running emergency cleanup before deciding to skip.",
            file=sys.stderr,
        )
        _run_emergency_cleanup(spec.agent)
        status = disk_pressure_status()
        if not status["critical"]:
            print(
                f"[{spec.agent.upper()}-DISK-RECOVERED] "
                f"emergency cleanup freed enough space "
                f"(free={status['free_gb']:.1f}GB, {status['free_pct']:.1f}%); proceeding.",
                file=sys.stderr,
            )
            return
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    # Still critical after cleanup: skip this firing cleanly.
    sentinel = f"[{spec.agent.upper()}-DISK-SKIP]"
    headline = (
        f"disk critically low: {status['free_gb']:.1f}GB free "
        f"({status['free_pct']:.1f}%); skipping firing to avoid ENOSPC"
    )
    print(f"{sentinel} {headline}")

    suppress_slack = (
        _env_value_enabled("ALFRED_DOCTOR")
        or spec.agent == "test"
        or (
            not _env_value_enabled("XPC_SERVICE_NAME")
            and not _env_value_enabled("ALFRED_PREFLIGHT_FORCE_SLACK")
        )
    )
    if not suppress_slack and _should_post_disk_slack(spec.agent):
        slack_post(
            f"💾 {spec.agent} skipped: {headline}. Emergency cleanup ran but "
            "could not free enough space — free disk on the host.",
            severity="warn",
        )
        _record_disk_slack_post(spec.agent)

    raise PreflightFailed([headline])


# --------------------------------------------------------------------------
# Optional shared-agent brain
#
# These wrap an optional shared-agent brain mounted at
# ``${ALFRED_HOME}/shared/.agent/``. Existing agents work unchanged; new
# agents can opt in with one line each. The brain is intentionally
# external to alfred-os so public installs stay small.
# --------------------------------------------------------------------------


def _shared_agent_available() -> bool:
    """Return True iff the brain is mounted and the core modules import.

    Conservative: any failure returns False so a missing or broken brain
    does NOT take down a working agent that opted in. Brain is an
    enhancement, not a load-bearing dependency.
    """
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
    """Return a formatted block of relevant past lessons for ``intent``.

    Drop the return value into the prompt of :func:`claude_invoke` so the
    model starts with prior knowledge. Returns the empty string if the
    brain is unavailable; never raises.
    """
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

    Use after every meaningful action so the dream cycle has something
    to cluster on. Returns the written entry or ``None`` when the brain
    is unavailable; never raises.
    """
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


def call_with_guardrail(prompt: str, validator: Any, **kwargs: Any) -> Any:
    """Invoke claude with output validation and reject-and-retry.

    Thin wrapper around ``harness/guardrail.py:with_guardrail`` so
    agents can use the pattern with one import. ``validator`` is either
    a callable ``(output) -> (bool, reason|None)`` or a ``Guardrail``
    instance. ``**kwargs`` are forwarded to :func:`claude_invoke`. The
    optional ``max_retries`` int (default 1) is consumed before forwarding.

    Returns ``None`` if the shared brain is not mounted; caller should
    fall back to plain :func:`claude_invoke`.
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
    """Build the brain context string for ``intent``. Empty when brain is absent."""
    if not _shared_agent_available():
        return ""
    try:
        from context_budget import build_context

        ctx, _used = build_context(intent, budget=budget)
        return ctx
    except Exception as e:
        print(f"[assemble_shared_context] swallowed: {e}", file=sys.stderr)
        return ""


# --------------------------------------------------------------------------
# Event-stream helpers (additive, opt-in per agent)
# --------------------------------------------------------------------------


def emit(event_type: str, **payload: Any) -> None:
    """Append one event to the shared stream. Best-effort, no-throw.

    Pull-out fields (``agent``, ``tokens_in``, ``tokens_out``,
    ``cost_usd``, ``tags``) are promoted to top-level columns; the rest
    becomes the event's payload dict.
    """
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
    """Context manager yielded by :func:`emit_firing`.

    ``success()`` records the happy-path numbers stamped on the closing
    event. If the ``with`` block raises, we emit a typed ``error``
    event AND ``firing_end(success=False)`` with the exception name.
    """

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self._num_turns: int = 0
        self._cost_usd: float = 0.0
        self._extra: dict = {}
        self._success_called: bool = False

    def success(self, *, num_turns: int = 0, cost_usd: float = 0.0, **extra: Any) -> None:
        """Record happy-path numbers for the closing event."""
        self._success_called = True
        self._num_turns = int(num_turns or 0)
        self._cost_usd = float(cost_usd or 0.0)
        self._extra = dict(extra)

    def __enter__(self) -> _FiringContext:
        emit("firing_start", agent=self.agent)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
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
    """
    return _FiringContext(agent)


# --------------------------------------------------------------------------
# Best-of-N helper (additive, opt-in)
# --------------------------------------------------------------------------


def best_of_n(
    agent: str,
    n: int = 2,
    *,
    task_id: str | None = None,
    work_factory: Any | None = None,
) -> Any | None:
    """Return a configured ``TaskRun`` for ``agent``, or ``None`` if brain absent.

    ``work_factory`` is required before calling ``run_attempts``; pass
    here or set ``run.work_factory`` on the returned object.
    ``task_id`` defaults to a fresh UUID4.
    """
    if not _shared_agent_available():
        return None
    try:
        from best_of_n import TaskRun, new_task_id  # type: ignore[import-not-found]
    except Exception as e:
        print(f"[best_of_n] swallowed import error: {e}", file=sys.stderr)
        return None

    def _placeholder_factory(_placement: int) -> Any:
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


# --------------------------------------------------------------------------
# LLM tier routing
#
# Issues / tasks declare which model handles them via the
# ``llm-tier:<x>`` label. Runners read the tier when picking work and
# call :func:`route_llm` instead of :func:`claude_invoke` directly.
# --------------------------------------------------------------------------
TIER_TO_MODEL: dict[str, str | None] = {
    "opus": "opus",
    "sonnet": "sonnet",
    "haiku": "haiku",
    "local": None,  # routed to Ollama, see _ollama_invoke
    "codex": None,  # routed to codex_invoke
}

OLLAMA_HOST = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:3b-instruct-q4_K_M"
OLLAMA_TIMEOUT_SEC = 30

_OLLAMA_HONORED_KWARGS = {"timeout"}
_OLLAMA_UNSUPPORTED_KWARGS = {
    "workdir",
    "allowed_tools",
    "max_turns",
    "resume_session",
    "model",
    "output_format",
}


def get_tier_from_labels(labels: list) -> str:
    """Read ``llm-tier:<x>`` from a list of GitHub label objects.

    Each label is a dict like ``{"name": "...", "color": "...", ...}``.
    The first matching ``llm-tier`` label wins so callers don't have to
    reason about ordering. Defaults to ``"sonnet"`` when no label is
    present.
    """
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

    Returns ``True`` if Ollama is reachable after the call (whether we
    started it or it was already running). Returns ``False`` if Ollama
    is not installed or the daemon never came up.
    """
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
    for _ in range(10):
        time.sleep(0.5)
        if _ollama_health_ok():
            return True
    return False


def _ollama_invoke(prompt: str, **kw: Any) -> ClaudeResult:
    """POST to Ollama ``/api/generate`` and return a ``ClaudeResult``-shaped reply.

    Returns a failure ``ClaudeResult`` when Ollama is not running or the
    request errors, so the caller can fall back to :func:`claude_invoke`
    without branching on tier. Kwargs that have no Ollama analogue are
    rejected up front so the caller does not believe a tool gate or
    session resume was enforced when it wasn't.
    """
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
    payload = json.dumps({"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}).encode("utf-8")
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


def route_llm(tier: str, prompt: str, **kw: Any) -> ClaudeResult:
    """Route a prompt to the right model based on tier.

    ``tier`` in ``{"opus", "sonnet", "haiku", "local", "codex"}``.
    Unknown tiers fall back to sonnet so a typo in a label can't take
    an agent down.

    For ``local``, ensures the Ollama daemon is up before dispatching
    so a cold host does not produce a misleading ``ollama not running``
    failure on the first call.
    """
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
