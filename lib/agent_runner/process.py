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
import secrets
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import (
    HYBRID_FALLBACK_SUBTYPES,
    dry_run_log,
    is_dry_run,
    normalize_engine,
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
    import os

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
        # Defensive: Python 3.14 may return bytes for ``e.stdout`` even when
        # ``text=True`` was passed to ``subprocess.run``. Coerce so downstream
        # consumers that expect str (e.g. ``Path.write_text``) do not crash.
        raw = e.stdout
        partial: str = (
            raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
        )
        return subprocess.CompletedProcess(
            cmd, 124, stdout=partial, stderr=f"TIMEOUT after {timeout}s"
        )
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

    Two transports are supported, selected by env var:

    1. ``ALFRED_CLAUDE_PROXY_SOCKET`` set + socket reachable -> route the
       invocation through ``claude-proxy``, the long-running unix-socket
       daemon documented in ``docs/CLAUDE_PROXY.md``. The proxy lives in
       the Aqua session so its spawned ``claude`` child inherits Keychain
       access; this is the workaround for the macOS launchd Keychain ACL
       problem described in ``docs/MACOS_KEYCHAIN.md``.
    2. Otherwise -> direct subprocess via :func:`claude_invoke`, the
       legacy default.

    The proxy path is best-effort: if the connection fails for any
    reason (stale socket, daemon restarting, peer-uid mismatch) the
    function silently falls back to the direct subprocess path so the
    caller never sees a transport-layer error. The ``agent`` and
    ``firing_id`` kwargs are still accepted for forward compatibility
    with a future per-firing JSONL transcript writer.
    """
    if max_turns is None:
        max_turns = _CLAUDE_UNLIMITED_TURNS

    proxy_result = _try_invoke_via_proxy(
        prompt,
        workdir=workdir,
        allowed_tools=allowed_tools,
        max_turns=max_turns,
        timeout=timeout,
        resume_session=resume_session,
        model=model,
        session_id=firing_id,
    )
    if proxy_result is not None:
        return proxy_result

    return claude_invoke(
        prompt,
        workdir=workdir,
        allowed_tools=allowed_tools,
        max_turns=max_turns,
        timeout=timeout,
        resume_session=resume_session,
        model=model,
    )


def _try_invoke_via_proxy(
    prompt: str,
    *,
    workdir: Path,
    allowed_tools: str,
    max_turns: int,
    timeout: int,
    resume_session: str | None,
    model: str | None,
    session_id: str,
) -> ClaudeResult | None:
    """Attempt the proxy transport; return ``None`` to signal fallback.

    Returns:
        A :class:`ClaudeResult` on success (proxy spawned ``claude``, we
        parsed its stream-JSON output), ``None`` when the proxy is not
        available or the connection failed. Never raises.
    """
    # Import locally so the OSS framework still works in environments where
    # ``claude_proxy`` is absent (vendored installs, partial copies). The
    # two-form try keeps both production (lib/ on sys.path, bin scripts)
    # and the test harness (repo root on sys.path) working without forcing
    # the suite to reach into module internals; either path resolves the
    # same file. The earlier ``from lib.claude_proxy ...`` only matched
    # the test layout, so under launchd this fell through to fallback and
    # the proxy never engaged — exactly the Keychain workaround the proxy
    # was built for.
    # Dual-import: try the deployed shape (`claude_proxy.X` with lib/ on
    # sys.path) first, fall back to the test-harness shape
    # (`lib.claude_proxy.X` with repo root on sys.path), then degrade to
    # direct subprocess. Both branches resolve the same file at runtime.
    # mypy handles the dual resolution via the `claude_proxy*` /
    # `lib.claude_proxy*` follow_imports = "skip" override in pyproject.
    try:
        from claude_proxy.client import (
            ProxyUnavailable,
            invoke_collected,
            proxy_available,
        )
        from claude_proxy.protocol import InvokeRequest
    except ImportError:
        try:
            from lib.claude_proxy.client import (
                ProxyUnavailable,
                invoke_collected,
                proxy_available,
            )
            from lib.claude_proxy.protocol import InvokeRequest
        except ImportError:
            return None

    if not proxy_available():
        return None

    request = InvokeRequest(
        prompt=prompt,
        workdir=str(workdir),
        allowed_tools=allowed_tools,
        session_id=session_id,
        claude_args=["--permission-mode", "bypassPermissions"],
        timeout_seconds=timeout,
        max_turns=max_turns,
        model=model,
        resume_session=resume_session,
    )
    try:
        stream = invoke_collected(request)
    except ProxyUnavailable:
        return None
    except OSError:
        return None

    return _claude_result_from_proxy_events(stream.events, fallback_exit=stream.exit_code)


def _claude_result_from_proxy_events(events: list[dict], fallback_exit: int) -> ClaudeResult:
    """Translate a list of stream-json events into a ClaudeResult.

    The upstream ``claude --output-format stream-json`` emits a final
    ``result`` event with the same envelope shape ``claude --output-format
    json`` would have produced; we pick that out and reuse the existing
    parser. If the stream ended before a ``result`` event arrived (proxy
    error, timeout, child killed) we synthesize a parse-failed envelope
    so the caller's retry / classification logic keeps working.
    """
    result_event: dict | None = None
    proxy_error: dict | None = None
    for ev in events:
        kind = ev.get("type")
        if kind == "result":
            result_event = ev
        elif kind == "proxy.error":
            proxy_error = ev

    if result_event is not None:
        return _build_claude_result(
            result_event,
            fallback_text=(proxy_error or {}).get("detail", ""),
        )

    detail = (
        (proxy_error or {}).get("detail")
        or (proxy_error or {}).get("reason")
        or (f"claude exited {fallback_exit} without a result event")
    )
    return ClaudeResult(
        success=False,
        subtype="parse-failed",
        num_turns=0,
        cost_usd=0.0,
        session_id=None,
        result_text=detail,
        raw={},
        stop_reason="error",
        error_message=detail,
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
            bypass_approvals_and_sandbox=codex_bypass_approvals_and_sandbox,
            add_dirs=codex_add_dirs,
        )

    if mode == "codex":
        return _invoke_codex(), "codex"

    result = _invoke_claude()
    if mode == "hybrid" and result.subtype in HYBRID_FALLBACK_SUBTYPES:
        if on_fallback:
            on_fallback(result)
        return _invoke_codex(), "codex-fallback"
    return result, "claude"
