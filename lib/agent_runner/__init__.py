"""alfred-os: shared library for host-scheduled Claude Code and Codex agents.

This is the public entry point. The library is internally factored into
nine focused modules (see :mod:`lib.agent_runner.paths`,
:mod:`lib.agent_runner.process`, etc.), but every name a runner needs
is re-exported here so the historical ``from agent_runner import X``
imports keep working unchanged.

High-level groupings:

* **Paths and binaries**: ``ALFRED_HOME``, ``WORKSPACE_ROOT``,
  ``WORKSPACE``, ``STATE_ROOT``, ``WORKTREE_ROOT``, ``CLAUDE_BIN``,
  ``CODEX_BIN``, ``GH_ORG``, ``now_iso``, ``today_str``.
* **Config**: ``env_int``, ``optional_env_int``, ``normalize_engine``,
  ``agent_engine``, ``engine_preflight_bins``,
  ``codex_sandbox_for_agent``, ``doctor_mode``, ``is_dry_run``,
  ``set_dry_run``, ``dry_run_log``.
* **Subprocess + invocations**: ``run``, ``gh_json``, ``short``,
  ``claude_invoke``, ``claude_invoke_streaming``, ``codex_invoke``,
  ``invoke_agent_engine``.
* **Result**: ``ClaudeResult``, ``STOP_REASON_HEALTHY``,
  ``STOP_REASON_FAIL``, ``dry_run_claude_result``.
* **State**: ``AgentLock``, ``with_lock``, ``SpendState``,
  ``EventLog``, ``is_globally_blocked``, ``set_global_block``,
  ``enable_agent``, ``disable_agent``, ``is_agent_enabled``,
  ``list_enabled_agents``.
* **GitHub**: ``STANDARD_LABELS``, ``LIFECYCLE_LABELS``,
  ``ensure_labels``, ``gh_pr_create``, ``gh_issue_edit``,
  ``gh_issue_comment``, ``gh_pr_comment``,
  ``find_open_authored_pr_for_issue``, ``claim_issue``,
  ``release_issue``, ``find_stale_claims``, ``force_release_stale_claim``,
  ``issue_dedup_check``, ``is_repo_paused``, ``list_paused_repos``,
  ``set_repo_paused``, ``make_worktree``, ``remove_worktree``,
  ``find_existing_worktree``, ``reuse_or_make_worktree``,
  ``make_worktree_from_branch``, ``push_current_branch``,
  ``worktree_risk_reason``, ``create_recovery_ref``, ``GH_REPO_TO_LOCAL``.
* **Notify**: ``slack_post``, ``SLACK_SEVERITY_INFO``,
  ``SLACK_SEVERITY_WARN``, ``SLACK_SEVERITY_ALERT``.
* **Metadata**: ``agent_role``, ``codename_with_role``,
  ``commit_trailer``, ``HandoffTable``, ``HANDOFFS``, ``load_prompt``.
* **Transcripts**: ``transcript_path``, ``codex_artifact_paths``.
* **Runtime memory**: ``parse_memory_reflections``,
  ``strip_memory_reflections``, ``format_memory_context``.
* **Orchestrator**: ``preflight``, ``PreflightSpec``,
  ``PreflightFailed``, ``route_llm``, ``get_tier_from_labels``,
  ``recall_for``, ``reflect``, ``call_with_guardrail``,
  ``assemble_shared_context``, ``emit``, ``emit_firing``, ``best_of_n``.

The private leading-underscore symbols re-exported here
(``_full_repo``, ``_parse_claim_comment``, ``_build_claude_result``,
``_should_retry_claude_auth``, ``_quarantine_stale_claude_credentials``,
the result regexes) are imported by the existing test suite and so
remain importable, but they are not part of the stable public surface.
"""

# This file is a re-export hub: every name here is intentionally imported
# for the side-effect of binding it into the ``agent_runner`` namespace.
# Tell ruff to stop complaining that the imports look unused.
# ruff: noqa: F401, E402, RUF022

from __future__ import annotations

# --------------------------------------------------------------------------
# State (locks, spend, fleet, events, global block)
# --------------------------------------------------------------------------
from .agent_events import (
    KNOWN_EVENT_TYPES,
    START_TYPES,
    TERMINAL_TYPES,
    Event,
    EventPayloadError,
    EventType,
    UnknownEventType,
    coerce_type,
    parse_record,
)

# --------------------------------------------------------------------------
# Config (env, engine, dry-run, doctor)
# --------------------------------------------------------------------------
from .config import (
    ENGINE_CHOICES,
    HYBRID_FALLBACK_SUBTYPES,
    PROVIDER_LIMIT_SUBTYPES,
    _agent_env_slug,
    _env_present,
    _env_value_enabled,
    _truthy_env,
    agent_engine,
    codex_sandbox_for_agent,
    doctor_mode,
    dry_run_log,
    engine_preflight_bins,
    env_int,
    is_dry_run,
    normalize_engine,
    optional_env_int,
    reported_subtype,
    set_dry_run,
)

# --------------------------------------------------------------------------
# Disk-pressure probe (ENOSPC guard)
# --------------------------------------------------------------------------
from .disk import (
    DEFAULT_MIN_FREE_DISK_GB,
    DEFAULT_MIN_FREE_DISK_PCT,
    DiskPressure,
    disk_pressure_status,
)

# --------------------------------------------------------------------------
# GitHub (labels, PRs, issues, claim/release, worktrees, paused repos)
# --------------------------------------------------------------------------
from .github import (
    CLAIM_COMMENT_PREFIX,
    GH_REPO_TO_LOCAL,
    LIFECYCLE_LABELS,
    RELEASE_COMMENT_PREFIX,
    STANDARD_LABELS,
    _detect_contested_claim,
    _full_repo,
    _issue_state,
    _make_dry_run_worktree,
    _parse_claim_comment,
    _worktree_branch,
    _worktree_is_stale,
    claim_issue,
    create_recovery_ref,
    ensure_labels,
    find_existing_worktree,
    find_open_authored_pr_for_issue,
    find_stale_claims,
    force_release_stale_claim,
    gh_issue_comment,
    gh_issue_edit,
    gh_pr_comment,
    gh_pr_create,
    is_repo_paused,
    issue_dedup_check,
    list_paused_repos,
    local_repo_dir,
    make_worktree,
    make_worktree_from_branch,
    push_current_branch,
    release_issue,
    remove_worktree,
    reuse_or_make_worktree,
    set_repo_paused,
    worktree_risk_reason,
)

# --------------------------------------------------------------------------
# Runtime memory helpers
# --------------------------------------------------------------------------
from .memory_runtime import (
    BEGIN_MARKER,
    END_MARKER,
    MemoryReflection,
    format_memory_context,
    load_runtime_memory,
    memory_reflection_instructions,
    parse_memory_reflections,
    record_firing,
    record_reflections,
    strip_memory_reflections,
    with_memory_prompt,
)

# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------
from .metadata import (
    HANDOFFS,
    HandoffTable,
    agent_role,
    codename_with_role,
    commit_trailer,
    load_prompt,
)

# --------------------------------------------------------------------------
# Notify
# --------------------------------------------------------------------------
from .notify import (
    _SLACK_SEVERITIES,
    SLACK_SEVERITY_ALERT,
    SLACK_SEVERITY_INFO,
    SLACK_SEVERITY_WARN,
    slack_post,
)

# --------------------------------------------------------------------------
# Orchestrator (preflight, brain shims, event stream, best-of-N, tier routing)
# --------------------------------------------------------------------------
from .orchestrator import (
    OLLAMA_HOST,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT_SEC,
    TIER_TO_MODEL,
    PreflightFailed,
    PreflightSpec,
    _disk_preflight_gate,
    _FiringContext,
    _ollama_health_ok,
    _ollama_invoke,
    _run_emergency_cleanup,
    _shared_agent_available,
    assemble_shared_context,
    best_of_n,
    call_with_guardrail,
    emit,
    emit_firing,
    get_tier_from_labels,
    preflight,
    recall_for,
    reflect,
    route_llm,
    start_ollama_if_needed,
    sync_checkout_to_default,
)

# --------------------------------------------------------------------------
# Paths + binaries + tiny datetime helpers
# --------------------------------------------------------------------------
from .paths import (
    ALFRED_HOME,
    BIN_DIR,
    CLAUDE_BIN,
    CODEX_APPROVAL_POLICY,
    CODEX_BIN,
    CODEX_DEFAULT_MODEL,
    CODEX_DEFAULT_SANDBOX,
    CODEX_TRANSCRIPTS_ROOT,
    FLEET_DIR,
    FLEET_ENABLED_FILE,
    GH_ORG,
    GLOBAL_BLOCKED_FILE,
    HOME,
    LIB_DIR,
    PAUSED_REPOS_FILE,
    PROMPTS_ROOT,
    SHARED_AGENT,
    SLACK_WEBHOOK_CACHE,
    SLACK_WEBHOOK_CACHE_TTL,
    STATE_ROOT,
    TRANSCRIPTS_ROOT,
    WORKSPACE,
    WORKSPACE_ROOT,
    WORKTREE_ROOT,
    WORKTREES_ROOT,
    now_iso,
    today_str,
)

# --------------------------------------------------------------------------
# Process + invocation
# --------------------------------------------------------------------------
from .process import (
    _CLAUDE_UNLIMITED_TURNS,
    claude_invoke,
    claude_invoke_streaming,
    codex_invoke,
    gh_json,
    invoke_agent_engine,
    pid_start_key,
    run,
    short,
)

# --------------------------------------------------------------------------
# Reliability: classification, retry/backoff, circuit breaker, loop detection
# --------------------------------------------------------------------------
from .reliability import (
    BreakerStatus,
    CircuitBreaker,
    FailureClass,
    LoopDetector,
    classify_exception,
    classify_result,
    compute_backoff_delay,
    retry_after_seconds,
    retry_with_backoff,
    step_fingerprint,
)

# --------------------------------------------------------------------------
# Result classification
# --------------------------------------------------------------------------
from .result import (
    _AUTH_RESULT_RE,
    _BUDGET_RESULT_RE,
    _OVERLOAD_RESULT_RE,
    _RATE_LIMIT_RESULT_RE,
    STOP_REASON_FAIL,
    STOP_REASON_HEALTHY,
    ClaudeResult,
    _build_claude_result,
    _claude_credentials_file,
    _derive_success,
    _quarantine_stale_claude_credentials,
    _should_retry_claude_auth,
    dry_run_claude_result,
)
from .state import (
    _LOCK_GRACE_SECONDS,
    PAUSE_MARKER_DIR,
    AgentLock,
    EventLog,
    SpendState,
    _atomic_write,
    _read_enabled_codenames,
    _write_enabled_codenames,
    agent_pause_marker_path,
    clear_agent_pause_marker,
    disable_agent,
    enable_agent,
    is_agent_enabled,
    is_agent_paused,
    is_globally_blocked,
    list_enabled_agents,
    lock_pid_identity_matches,
    lock_pid_identity_status,
    maybe_set_global_block_for_result,
    reset_consecutive_failures,
    set_global_block,
    with_lock,
    write_agent_pause_marker,
)
from .tool_digest import (
    ToolDigest,
    digest_diff,
    digest_test_log,
    digest_tool_output,
)

# --------------------------------------------------------------------------
# Transcripts
# --------------------------------------------------------------------------
from .transcripts import (
    _extract_codex_session_id,
    _extract_codex_tokens,
    codex_artifact_paths,
    transcript_path,
)

__all__ = [
    # paths
    "ALFRED_HOME",
    "BIN_DIR",
    "CLAUDE_BIN",
    "CODEX_APPROVAL_POLICY",
    "CODEX_BIN",
    "CODEX_DEFAULT_MODEL",
    "CODEX_DEFAULT_SANDBOX",
    "CODEX_TRANSCRIPTS_ROOT",
    "FLEET_DIR",
    "FLEET_ENABLED_FILE",
    "GH_ORG",
    "GLOBAL_BLOCKED_FILE",
    "HOME",
    "LIB_DIR",
    "PAUSED_REPOS_FILE",
    "PROMPTS_ROOT",
    "SHARED_AGENT",
    "SLACK_WEBHOOK_CACHE",
    "SLACK_WEBHOOK_CACHE_TTL",
    "STATE_ROOT",
    "TRANSCRIPTS_ROOT",
    "WORKSPACE",
    "WORKSPACE_ROOT",
    "WORKTREE_ROOT",
    "WORKTREES_ROOT",
    "now_iso",
    "today_str",
    # config
    "ENGINE_CHOICES",
    "HYBRID_FALLBACK_SUBTYPES",
    "PROVIDER_LIMIT_SUBTYPES",
    "agent_engine",
    "codex_sandbox_for_agent",
    "doctor_mode",
    "dry_run_log",
    "engine_preflight_bins",
    "env_int",
    "is_dry_run",
    "normalize_engine",
    "optional_env_int",
    "reported_subtype",
    "set_dry_run",
    # reliability
    "BreakerStatus",
    "CircuitBreaker",
    "FailureClass",
    "LoopDetector",
    "classify_exception",
    "classify_result",
    "compute_backoff_delay",
    "retry_after_seconds",
    "retry_with_backoff",
    "step_fingerprint",
    # result
    "ClaudeResult",
    "STOP_REASON_FAIL",
    "STOP_REASON_HEALTHY",
    "dry_run_claude_result",
    # process / invocations
    "claude_invoke",
    "claude_invoke_streaming",
    "codex_invoke",
    "gh_json",
    "invoke_agent_engine",
    "pid_start_key",
    "run",
    "short",
    # transcripts
    "codex_artifact_paths",
    "transcript_path",
    # metadata
    "HANDOFFS",
    "HandoffTable",
    "agent_role",
    "codename_with_role",
    "commit_trailer",
    "load_prompt",
    # notify
    "SLACK_SEVERITY_ALERT",
    "SLACK_SEVERITY_INFO",
    "SLACK_SEVERITY_WARN",
    "slack_post",
    # state
    "AgentLock",
    "EventLog",
    "Event",
    "EventType",
    "EventPayloadError",
    "UnknownEventType",
    "KNOWN_EVENT_TYPES",
    "START_TYPES",
    "TERMINAL_TYPES",
    "coerce_type",
    "parse_record",
    "PAUSE_MARKER_DIR",
    "SpendState",
    "agent_pause_marker_path",
    "clear_agent_pause_marker",
    "disable_agent",
    "enable_agent",
    "is_agent_enabled",
    "is_agent_paused",
    "is_globally_blocked",
    "list_enabled_agents",
    "lock_pid_identity_matches",
    "lock_pid_identity_status",
    "maybe_set_global_block_for_result",
    "reset_consecutive_failures",
    "set_global_block",
    "with_lock",
    "write_agent_pause_marker",
    # github
    "CLAIM_COMMENT_PREFIX",
    "GH_REPO_TO_LOCAL",
    "LIFECYCLE_LABELS",
    "RELEASE_COMMENT_PREFIX",
    "STANDARD_LABELS",
    "claim_issue",
    "create_recovery_ref",
    "ensure_labels",
    "find_existing_worktree",
    "find_open_authored_pr_for_issue",
    "find_stale_claims",
    "local_repo_dir",
    "force_release_stale_claim",
    "gh_issue_comment",
    "gh_issue_edit",
    "gh_pr_comment",
    "gh_pr_create",
    "is_repo_paused",
    "issue_dedup_check",
    "list_paused_repos",
    "make_worktree",
    "make_worktree_from_branch",
    "push_current_branch",
    "release_issue",
    "remove_worktree",
    "reuse_or_make_worktree",
    "set_repo_paused",
    "worktree_risk_reason",
    # runtime memory
    "BEGIN_MARKER",
    "END_MARKER",
    "MemoryReflection",
    "format_memory_context",
    "load_runtime_memory",
    "memory_reflection_instructions",
    "parse_memory_reflections",
    "record_firing",
    "record_reflections",
    "strip_memory_reflections",
    "with_memory_prompt",
    # tool-output digest
    "ToolDigest",
    "digest_diff",
    "digest_test_log",
    "digest_tool_output",
    # orchestrator
    "OLLAMA_HOST",
    "OLLAMA_MODEL",
    "OLLAMA_TIMEOUT_SEC",
    "PreflightFailed",
    "PreflightSpec",
    "TIER_TO_MODEL",
    "assemble_shared_context",
    "best_of_n",
    "call_with_guardrail",
    "emit",
    "emit_firing",
    "get_tier_from_labels",
    "preflight",
    "recall_for",
    "reflect",
    "route_llm",
    "start_ollama_if_needed",
    "sync_checkout_to_default",
    # disk
    "DEFAULT_MIN_FREE_DISK_GB",
    "DEFAULT_MIN_FREE_DISK_PCT",
    "DiskPressure",
    "disk_pressure_status",
]


# --------------------------------------------------------------------------
# Test-friendly attribute propagation
#
# The monolithic ``agent_runner.py`` was a single module, so existing
# tests patch attributes on it directly::
#
#     monkeypatch.setattr(ar, "slack_post", fake)
#     monkeypatch.setattr(ar, "subprocess", fake_subprocess)
#
# After the split, those names also live inside submodules
# (``notify.slack_post``, ``process.subprocess``, ...). A bare
# ``setattr`` on the package only rebinds the symbol in the
# ``__init__`` namespace, not in the submodule the consumer call site
# imported from - so the monkeypatch silently has no effect.
#
# To preserve drop-in compatibility we subclass ``ModuleType``: any
# ``setattr`` on this package fans out to every submodule that already
# carried the same attribute name, plus a few legacy aliases the test
# suite expects (``subprocess`` -> ``process.subprocess``).
# --------------------------------------------------------------------------
import subprocess as _subprocess_module
from types import ModuleType as _ModuleType

# Legacy alias: the monolith re-exported the stdlib ``subprocess`` module
# itself, and at least one test patches via ``ar.subprocess.run``. Make
# sure the attribute is present.
subprocess = _subprocess_module

# Direct references to the submodules so the propagation hook is
# immune to ``sys.modules`` churn (tests in this codebase delete
# ``agent_runner.*`` from sys.modules to force a fresh import).
from . import (
    config as _sub_config,
)
from . import (
    disk as _sub_disk,
)
from . import (
    github as _sub_github,
)
from . import (
    memory_runtime as _sub_memory_runtime,
)
from . import (
    metadata as _sub_metadata,
)
from . import (
    notify as _sub_notify,
)
from . import (
    orchestrator as _sub_orchestrator,
)
from . import (
    paths as _sub_paths,
)
from . import (
    process as _sub_process,
)
from . import (
    result as _sub_result,
)
from . import (
    state as _sub_state,
)
from . import (
    transcripts as _sub_transcripts,
)

_SUBMODULE_OBJS: tuple[_ModuleType, ...] = (
    _sub_paths,
    _sub_config,
    _sub_disk,
    _sub_process,
    _sub_result,
    _sub_transcripts,
    _sub_memory_runtime,
    _sub_metadata,
    _sub_notify,
    _sub_state,
    _sub_github,
    _sub_orchestrator,
)

# Cache of attribute name -> list of submodule objects that define it.
# Populated lazily on first patch.
_PROPAGATION_TARGETS: dict[str, list[_ModuleType]] = {}


def _build_propagation_targets(name: str) -> list[_ModuleType]:
    """Return submodule objects that already define ``name``."""
    return [mod for mod in _SUBMODULE_OBJS if hasattr(mod, name)]


class _AgentRunnerModule(_ModuleType):
    """Module subclass that propagates ``setattr`` to source submodules."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name.startswith("_AgentRunnerModule__") or name in {
            "__dict__",
            "__class__",
        }:
            return
        targets = _PROPAGATION_TARGETS.get(name)
        if targets is None:
            targets = _build_propagation_targets(name)
            _PROPAGATION_TARGETS[name] = targets
        import contextlib

        for mod in targets:
            with contextlib.suppress(AttributeError, TypeError):
                object.__setattr__(mod, name, value)


# Install the custom module class on this package.
import sys as _sys

_sys.modules[__name__].__class__ = _AgentRunnerModule


# --------------------------------------------------------------------------
# Optional fleet overlay
# --------------------------------------------------------------------------
# If the operator has placed a Python module on the import path that
# customises fleet-wide dicts (``GH_REPO_TO_LOCAL``, ``STANDARD_LABELS``,
# ``HANDOFFS``, etc.), import it here so its module-level side effects run
# before any consumer reads those dicts. Defaults to ``fleet_overlay``;
# override with the ``ALFRED_FLEET_OVERLAY`` env var. Silently absent when
# the module is missing (the OSS standalone case).
import importlib as _importlib
import importlib.util as _importlib_util
import os as _os

_overlay_name = _os.environ.get("ALFRED_FLEET_OVERLAY", "fleet_overlay")
# Distinguish "overlay not present" (silent, OSS-standalone case) from
# "overlay present but raises during import" (loud, operator bug we want
# to surface). ``find_spec`` returning ``None`` is the missing case; any
# exception from ``import_module`` after a spec was found is the broken
# case, and we let it propagate.
if _importlib_util.find_spec(_overlay_name) is not None:
    _importlib.import_module(_overlay_name)
