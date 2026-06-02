"""Subprocess wrappers and LLM CLI invocations.

This module owns the boundary between Python and the shell:

* :func:`run` — ``subprocess.run`` with sane defaults and no exceptions
  on timeout (returns a ``CompletedProcess`` with ``returncode=124``).
* :func:`gh_json` — call ``gh`` with ``--json`` and parse to ``dict``
  / ``list``; return a default on any failure.
* :func:`pid_start_key` — read ``ps -p ... lstart`` for lock-identity.
* :func:`short` — display-trim long output for logs.
* :func:`claude_invoke` and :func:`claude_invoke_streaming` —
  invoke the Claude Code CLI and parse its sentinel response.
* :func:`codex_invoke` — invoke the Codex CLI non-interactively and
  marshal its artefacts.
* :func:`invoke_agent_engine` — engine-aware dispatch for
  Claude / Codex / Claude-first hybrid with fallback.

What this module does NOT own:

* The ``ClaudeResult`` dataclass and envelope classification ->
  ``result.py``.
* Spend tracking or fleet ledgers -> ``state.py``.
* gh CLI helpers for PR / issue / label operations -> ``github.py``.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import signal
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import (
    HYBRID_FALLBACK_SUBTYPES,
    _truthy_env,
    dry_run_log,
    is_dry_run,
    normalize_engine,
)
from .memory_runtime import (
    BEGIN_MARKER,
    load_runtime_memory,
    parse_memory_reflections,
    record_firing,
    record_reflections,
    strip_memory_reflections,
    with_memory_prompt,
)
from .paths import (
    CLAUDE_BIN,
    CODEX_APPROVAL_POLICY,
    CODEX_BIN,
    CODEX_DEFAULT_MODEL,
    CODEX_DEFAULT_SANDBOX,
)
from .result import (
    _BUDGET_RESULT_RE,
    _RATE_LIMIT_RESULT_RE,
    ClaudeResult,
    _build_claude_result,
    _should_retry_claude_auth,
    dry_run_claude_result,
)
from .transcripts import (
    _extract_codex_session_id,
    _extract_codex_tokens,
    codex_artifact_paths,
)

# Claude Code's ``-p`` (non-interactive) mode applies a hidden 40-turn
# default when ``--max-turns`` is omitted. That default is far too tight
# for our agents (cross-file work routinely needs 60-150 turns), so
# ``claude_invoke`` always passes an explicit ``--max-turns``: the
# caller's value if given, otherwise this effectively-unlimited number.
# The per-firing wall-clock ``timeout`` becomes the real ceiling.
_CLAUDE_UNLIMITED_TURNS: int = 999

# Headless fleet agents run unattended under launchd, so a Claude Code
# desktop/push notification on every firing is pure noise (and on macOS it
# stacks up banners no one reads). We pass these settings via the CLI's
# ``--settings`` flag, which ADDS a settings source on top of the
# config-dir settings — it does NOT replace auth. Auth comes from the
# config-dir credentials (OAuth / keychain / CLAUDE_CODE_OAUTH_TOKEN), none
# of which live in a settings.json, so suppressing notifications here can
# never log the agent out. Opt back in (e.g. for interactive debugging)
# with ``ALFRED_AGENT_NOTIFICATIONS=1``.
_AGENT_NOTIF_SUPPRESS_SETTINGS = '{"agentPushNotifEnabled":false,"preferredNotifChannel":"none"}'


def _agent_notifications_enabled() -> bool:
    """True only when the operator explicitly re-enables agent notifications.

    Default is suppressed (the flag is added). Setting
    ``ALFRED_AGENT_NOTIFICATIONS=1`` (or true/yes/on) keeps notifications
    on by omitting the ``--settings`` suppression source.
    """
    return _truthy_env("ALFRED_AGENT_NOTIFICATIONS")


# Headless firings also run under ``--permission-mode bypassPermissions`` (full
# trust), so a deterministic PreToolUse hook is the only backstop that survives
# prompt drift. ``lib/alfred_hooks.py`` denies pushes to protected branches,
# destructive ``rm -rf`` outside the worktree, secret-file reads, ``curl|bash``
# pipelines, and (when ``ALFRED_SCRUB_NAMES`` is configured) writes of banned
# names. It is merged into the same ``--settings`` payload as the notification
# suppression. On by default; disable for a manual debug run with
# ``ALFRED_AGENT_HOOKS=0``.
def _agent_hooks_enabled() -> bool:
    """PreToolUse guardrails are OPT-IN; unrestricted ("YOLO") is the default.

    Alfred's value is unattended autonomy, so we do NOT impose guardrails by
    default. The hook is an optional deterministic backstop for anyone who wants
    one on a bypassPermissions fleet (e.g. a cautious first run on an unfamiliar
    repo). Turn it on with ``ALFRED_AGENT_HOOKS=1`` (true/yes/on).
    """
    return _truthy_env("ALFRED_AGENT_HOOKS")


def _agent_hook_settings() -> dict:
    """PreToolUse guardrail hook config, or ``{}`` when disabled/missing."""
    if not _agent_hooks_enabled():
        return {}
    # process.py lives at lib/agent_runner/process.py; the hook is lib/alfred_hooks.py.
    script = Path(__file__).resolve().parent.parent / "alfred_hooks.py"
    if not script.exists():
        return {}
    command = f'python3 "{script}" pretooluse'
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash|Read|Write|Edit|MultiEdit|NotebookEdit",
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
        }
    }


def _agent_settings_args() -> list[str]:
    """Single ``--settings`` payload: notification suppression + the hook.

    Returns ``[]`` only when both are opted out, keeping the command line clean.
    """
    settings: dict = {}
    if not _agent_notifications_enabled():
        settings.update({"agentPushNotifEnabled": False, "preferredNotifChannel": "none"})
    settings.update(_agent_hook_settings())
    if not settings:
        return []
    return ["--settings", json.dumps(settings, separators=(",", ":"))]


# ---------- Memory MCP attachment ----------
#
# bin/alfred-mcp.py is a stdio MCP server exposing read-only memory tools
# (recall, recent file touches, failure patterns, brain status) over the local
# brain. Attaching it to every firing lets agents recall prior lessons as a
# TOOL (the model decides when) instead of memory being a passive store the
# operator queries by hand. This is a capability, not a restriction, so it is
# on by default; disable with ALFRED_MEMORY_MCP=0.
MEMORY_MCP_SERVER = "alfred_memory"
_MEMORY_RECALL_TOOLS = (
    "alfred_memory_recall",
    "alfred_recent_file_touches",
    "alfred_failure_patterns",
    "alfred_brain_status",
)


def _memory_mcp_enabled() -> bool:
    val = os.environ.get("ALFRED_MEMORY_MCP")
    if val is None:
        return True
    return val.strip().lower() not in {"0", "false", "no", "off", ""}


def _memory_mcp_script() -> Path | None:
    # process.py is lib/agent_runner/process.py; the server is bin/alfred-mcp.py.
    script = Path(__file__).resolve().parents[2] / "bin" / "alfred-mcp.py"
    return script if script.exists() else None


def _memory_mcp_args() -> list[str]:
    """``--mcp-config`` args attaching the read-only memory server, or ``[]``.

    The server exposes only read-only tools (no arbitrary-query escape hatch),
    so no per-tool restriction is needed even under bypassPermissions.
    """
    if not _memory_mcp_enabled():
        return []
    script = _memory_mcp_script()
    if script is None:
        return []
    config = {
        "mcpServers": {MEMORY_MCP_SERVER: {"command": "python3", "args": [str(script), "serve"]}}
    }
    return ["--mcp-config", json.dumps(config, separators=(",", ":"))]


def _memory_tool_names() -> list[str]:
    return [f"mcp__{MEMORY_MCP_SERVER}__{t}" for t in _MEMORY_RECALL_TOOLS]


def _with_memory_mcp_tools(allowed_tools: str) -> str:
    """Append the read-only memory recall tools to an allowlist when enabled.

    Preserves the caller's separator style (comma vs space). No-op when the MCP
    is disabled or the server script is missing.
    """
    if not _memory_mcp_enabled() or _memory_mcp_script() is None:
        return allowed_tools
    base = (allowed_tools or "").strip()
    existing = set(base.replace(",", " ").split())
    additions = [n for n in _memory_tool_names() if n not in existing]
    if not additions:
        return base
    sep = "," if ("," in base or " " not in base) else " "
    return (base + sep if base else "") + sep.join(additions)


def _subprocess_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    """Terminate ``proc`` and its child process group after a timeout."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
        with contextlib.suppress(ProcessLookupError, OSError):
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=5)


def _popen_run_text(
    cmd: list[str],
    *,
    cwd: str | None = None,
    timeout: int = 60,
    capture: bool = True,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess in its own process group and reap it on timeout."""
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        env=env,
        start_new_session=(os.name != "nt"),
    )
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout or "", stderr or "")
    except subprocess.TimeoutExpired as exc:
        stdout = _subprocess_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
        stderr = _subprocess_text(getattr(exc, "stderr", None))
        _terminate_process_group(proc)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
            more_out, more_err = proc.communicate(timeout=1)
            stdout += _subprocess_text(more_out)
            stderr += _subprocess_text(more_err)
        timeout_msg = f"TIMEOUT after {timeout}s"
        stderr = f"{stderr}\n{timeout_msg}".strip() if stderr else timeout_msg
        return subprocess.CompletedProcess(cmd, 124, stdout=stdout, stderr=stderr)


def run(
    cmd: list[str],
    *,
    cwd: str | None = None,
    timeout: int = 60,
    check: bool = False,
    capture: bool = True,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Wrapped ``subprocess.run`` with sane defaults and clear errors.

    Args:
        cmd: argv list.
        cwd: working directory.
        timeout: wall-clock seconds before ``CompletedProcess(returncode=124)``.
        check: forwarded to ``subprocess.run``.
        capture: capture stdout/stderr as text.
        env: extra env vars merged on top of ``os.environ``.

    Returns:
        Always a ``subprocess.CompletedProcess``; timeouts and unknown
        exceptions are caught and surfaced via the return code instead
        of propagating.
    """
    proc_env = dict(os.environ)
    if env:
        proc_env.update(env)
    try:
        result = _popen_run_text(
            cmd,
            cwd=cwd,
            timeout=timeout,
            capture=capture,
            env=proc_env,
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                cmd,
                output=result.stdout,
                stderr=result.stderr,
            )
        return result
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"{type(e).__name__}: {e}")


def gh_json(cmd: list[str], default: Any = None) -> Any:
    """Run ``gh`` and parse JSON output; return ``default`` on any failure."""
    res = run(cmd, timeout=60)
    if res.returncode != 0:
        return default
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return default


def pid_start_key(pid: int) -> str:
    """Read ``ps -p <pid> -o lstart`` as the per-PID identity key.

    Used by lock-holder verification: a PID alone can be recycled, but
    ``lstart`` (start time) plus PID is unique on the host. Returns the
    empty string when ``ps`` is unavailable or the PID is gone.
    """
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


def short(text: str, n: int = 300) -> str:
    """Trim ``text`` to at most ``n`` characters with an ellipsis suffix."""
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "..."


# --------------------------------------------------------------------------
# Claude CLI invocation
# --------------------------------------------------------------------------


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
    """Invoke ``claude -p`` with the given prompt; return a parsed result.

    Uses ``--output-format json`` (single final event). On a one-time
    ``error_authentication`` classification we quarantine a stale
    ``~/.claude/.credentials.json`` (if any) and retry once, letting the
    CLI fall back to Keychain. ``_auth_retry`` is the re-entry guard,
    set internally on the retry call so we can never loop. Disabled
    entirely by ``ALFRED_DISABLE_CLAUDE_AUTH_REPAIR=1``.

    Args:
        prompt: full text passed via ``-p``.
        workdir: working directory for the subprocess.
        allowed_tools: comma-separated tool gate (forwarded to
            ``--allowedTools``).
        max_turns: explicit ceiling. ``None`` -> ``_CLAUDE_UNLIMITED_TURNS``
            so the CLI's hidden 40-turn default never bites.
        timeout: wall-clock seconds.
        resume_session: optional ``--resume`` session ID.
        model: optional ``--model`` alias forwarded to the CLI.

    Returns:
        A :class:`ClaudeResult` with both legacy (``success`` /
        ``subtype`` / ``num_turns`` / ``cost_usd`` / ``result_text``)
        and additive (``stop_reason`` / ``error_message``) fields.
    """
    if is_dry_run():
        dry_run_log(
            "llm",
            f"would invoke claude with prompt of {len(prompt)} chars, "
            f"model={model or '(cli-default)'}, "
            f"max_turns={max_turns if max_turns is not None else '(unlimited)'}",
        )
        return dry_run_claude_result(prompt, model=model, engine="claude")

    effective_max_turns = max_turns if max_turns is not None else _CLAUDE_UNLIMITED_TURNS
    cmd = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--allowedTools",
        _with_memory_mcp_tools(allowed_tools),
        "--max-turns",
        str(effective_max_turns),
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
    ]
    # One ``--settings`` source carrying notification suppression (default on,
    # opt out with ALFRED_AGENT_NOTIFICATIONS=1) AND the OPT-IN PreToolUse
    # guardrail hook (off by default; enable with ALFRED_AGENT_HOOKS=1).
    # ``--settings`` adds a source; it does not touch auth.
    cmd.extend(_agent_settings_args())
    # Attach the read-only memory MCP server so agents can recall lessons as a
    # tool (capability, on by default; ALFRED_MEMORY_MCP=0 to disable).
    cmd.extend(_memory_mcp_args())
    if model:
        cmd.extend(["--model", model])
    if resume_session:
        cmd.extend(["--resume", resume_session])

    res = run(cmd, cwd=str(workdir), timeout=timeout, capture=True)

    if res.returncode == 124:
        return ClaudeResult(
            success=False,
            subtype="error_timeout",
            num_turns=0,
            cost_usd=0.0,
            session_id=None,
            result_text=res.stdout or res.stderr or "",
            raw={"returncode": 124, "timeout": timeout},
            stop_reason="aborted",
            error_message=f"claude_invoke exceeded {timeout}s",
        )

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

    Historically this also routed through a local unix-socket daemon
    (``claude-proxy``) to work around a macOS Keychain ACL issue under
    launchd. Since the operator can instead expose
    ``CLAUDE_CODE_OAUTH_TOKEN`` (see ``docs/CLAUDE_CODE.md``) which makes
    ``claude`` skip Keychain entirely, the proxy was removed in v0.4.1.

    The wrapper survives because the ``agent`` and ``firing_id`` kwargs
    are part of the public call signature every codename agent uses;
    removing them would be a breaking ABI change. Both kwargs are
    currently unused (see :func:`claude_invoke` for the actual transport)
    but are reserved for downstream changes that need per-firing context
    without touching every caller.
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


# --------------------------------------------------------------------------
# Codex CLI invocation
# --------------------------------------------------------------------------


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
    bypass_approvals_and_sandbox: bool = False,
    add_dirs: list[Path] | None = None,
    allowed_tools: str | None = None,
    max_turns: int | None = None,
    resume_session: str | None = None,
) -> ClaudeResult:
    """Invoke ``codex exec`` non-interactively; return a ``ClaudeResult`` shape.

    Codex does not expose Claude's tool allow-list, max-turn, or
    resume-session semantics. The wrapper rejects those kwargs instead
    of implying they were enforced. Default posture is review-safe:
    read-only sandbox and no approval prompts.
    """
    if is_dry_run():
        dry_run_log(
            "llm",
            f"would invoke codex with prompt of {len(prompt)} chars, "
            f"model={model or CODEX_DEFAULT_MODEL or '(cli-default)'}, "
            f"sandbox={sandbox or CODEX_DEFAULT_SANDBOX}",
        )
        return dry_run_claude_result(prompt, model=model, engine="codex")

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
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        firing_id = f"{stamp}-{secrets.token_hex(2)}"

    paths = codex_artifact_paths(agent, firing_id)
    cmd = [
        CODEX_BIN,
        "exec",
        "--skip-git-repo-check",
        "--cd",
        str(workdir),
    ]
    resolved_sandbox = sandbox or CODEX_DEFAULT_SANDBOX
    if bypass_approvals_and_sandbox:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
        resolved_sandbox = "danger-full-access"
    else:
        cmd.extend(
            [
                "--sandbox",
                resolved_sandbox,
                "-c",
                f'approval_policy="{approval_policy or CODEX_APPROVAL_POLICY}"',
            ]
        )
    cmd.extend(["--output-last-message", str(paths["last_message"])])
    chosen_model = model or CODEX_DEFAULT_MODEL
    if chosen_model:
        cmd.extend(["--model", chosen_model])
    for directory in add_dirs or []:
        cmd.extend(["--add-dir", str(directory)])
    cmd.append("-")

    try:
        proc = _popen_run_text(
            cmd,
            cwd=str(workdir),
            timeout=timeout,
            capture=True,
            input_text=prompt,
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
    if proc.returncode == 124:
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        with contextlib.suppress(OSError):
            paths["stdout"].write_text(stdout)
            paths["stderr"].write_text(stderr)
        last_message = ""
        with contextlib.suppress(OSError):
            last_message = paths["last_message"].read_text().strip()
        combined = f"{stdout}\n{stderr}"
        return ClaudeResult(
            success=False,
            subtype="error_timeout",
            num_turns=0,
            cost_usd=0.0,
            session_id=_extract_codex_session_id(combined),
            result_text=last_message or stdout,
            raw={
                "engine": "codex",
                "returncode": 124,
                "stdout_path": str(paths["stdout"]),
                "stderr_path": str(paths["stderr"]),
                "last_message_path": str(paths["last_message"]),
                "tokens_used": _extract_codex_tokens(combined),
                "model": chosen_model,
                "sandbox": resolved_sandbox,
                "bypass_approvals_and_sandbox": bypass_approvals_and_sandbox,
                "timeout": timeout,
            },
            stop_reason="aborted",
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
        "sandbox": resolved_sandbox,
        "bypass_approvals_and_sandbox": bypass_approvals_and_sandbox,
    }
    session_id = _extract_codex_session_id(combined)
    if proc.returncode != 0:
        tail = (result_text or stderr or stdout or "").strip()[-1000:]
        classifier_text = f"{result_text}\n{stdout}\n{stderr}"
        subtype = "error_rate_limit" if _RATE_LIMIT_RESULT_RE.search(classifier_text) else "error"
        if subtype == "error" and _BUDGET_RESULT_RE.search(classifier_text):
            subtype = "error_rate_limit"
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


# --------------------------------------------------------------------------
# Engine-aware dispatch
# --------------------------------------------------------------------------


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
    codex_bypass_approvals_and_sandbox: bool = False,
    claude_fn: Callable[..., ClaudeResult] | None = None,
    codex_fn: Callable[..., ClaudeResult] | None = None,
    on_fallback: Callable[[ClaudeResult], None] | None = None,
    memory_repo: str | None = None,
    memory_query: str | None = None,
    memory_limit: int = 3,
) -> tuple[ClaudeResult, str]:
    """Invoke a prompt through Claude, Codex, or Claude-first hybrid.

    Returns ``(result, engine_used)`` where ``engine_used`` is one of
    ``"claude"``, ``"codex"``, or ``"codex-fallback"``. The
    ``on_fallback`` callback fires only when hybrid mode falls back
    after a Claude provider-limit subtype; useful for posting a
    one-line Slack warning.
    """
    mode = normalize_engine(engine)
    claude_call = claude_fn or claude_invoke_streaming
    codex_call = codex_fn or codex_invoke
    memory_provider = load_runtime_memory() if memory_repo else None
    prompt_for_engine = with_memory_prompt(
        prompt,
        memory_provider,
        codename=agent,
        repo=memory_repo,
        query=memory_query,
        limit=memory_limit,
    )

    def _invoke_claude() -> ClaudeResult:
        return claude_call(
            prompt_for_engine,
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
            prompt_for_engine,
            workdir=workdir,
            agent=agent,
            firing_id=firing_id,
            timeout=codex_timeout or timeout,
            model=codex_model,
            sandbox=codex_sandbox,
            approval_policy=codex_approval_policy,
            bypass_approvals_and_sandbox=codex_bypass_approvals_and_sandbox,
            add_dirs=codex_add_dirs,
        )

    if mode == "codex":
        result = _invoke_codex()
        engine_used = "codex"
    else:
        result = _invoke_claude()
        engine_used = "claude"
        if mode == "hybrid" and result.subtype in HYBRID_FALLBACK_SUBTYPES:
            if on_fallback:
                on_fallback(result)
            result = _invoke_codex()
            engine_used = "codex-fallback"

    if memory_provider is not None and memory_repo:
        result_text = result.result_text or ""
        reflections = parse_memory_reflections(result_text)
        if BEGIN_MARKER in result_text:
            result.result_text = strip_memory_reflections(result_text)
        if reflections:
            record_reflections(
                memory_provider,
                reflections,
                codename=agent,
                repo=memory_repo,
                firing_id=firing_id,
            )
        record_firing(
            memory_provider,
            codename=agent,
            repo=memory_repo,
            firing_id=firing_id,
            result=result,
            engine_used=engine_used,
        )
    return result, engine_used
