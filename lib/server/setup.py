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
import plistlib
import re
import shlex
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The watched-repo allowlist the rest of the fleet reads. The Set up surface
# writes BOTH the queue allowlist (controls what an operator can arm/hold/close)
# and the shipped allowlist (controls which repos the board scans), so the one
# golden-path repo pick wires up the whole experience, including the native
# Plan-work -> GitHub issue handoff and the Slack issue bridge.
QUEUE_REPOS_ENV = "ALFRED_QUEUE_REPOS"
SHIPPED_REPOS_ENV = "ALFRED_SHIPPED_REPOS"
BRIDGE_REPOS_ENV = "ALFRED_BRIDGE_REPOS"
_REPO_ENV_KEYS = (QUEUE_REPOS_ENV, SHIPPED_REPOS_ENV, BRIDGE_REPOS_ENV)
_BOARD_REPO_ENV_KEYS = (SHIPPED_REPOS_ENV, BRIDGE_REPOS_ENV)

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Engine CLIs Alfred rides. Detected by presence on PATH only (no version
# spawn): the golden path needs at least one of these signed-in subscription
# CLIs, never an API key paste.
_ENGINE_BINS = ("claude", "codex")
_FALSEY = {"0", "false", "no", "off", ""}
_CODE_MEMORY_BIN_NAME = "codebase-memory-mcp"
_CODE_MEMORY_LAUNCHER = Path(__file__).resolve().parents[2] / "bin" / "code-memory-mcp"
_CODE_MEMORY_VERSION_RE = re.compile(
    r'^CODE_MEMORY_VERSION="\$\{ALFRED_CODE_MEMORY_VERSION:-([^}]+)\}"'
)
_CODE_MEMORY_REPO_RE = re.compile(r'^CODE_MEMORY_REPO="\$\{ALFRED_CODE_MEMORY_REPO:-([^}]+)\}"')
_CODE_MEMORY_DISCOVERY_LIMIT = 25
_CODE_MEMORY_DISCOVERY_IGNORES = {
    ".archive",
    ".cache",
    ".external",
    ".external-submissions",
    ".venv",
    ".worktrees",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
}
_CODE_MEMORY_GRAPH_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_ALFRED_LAUNCHD_IDENTITY_TOKENS = frozenset({"alfred"})
_ALFRED_LAUNCHD_ROLE_TOKENS = frozenset(
    {
        "automerge",
        "bane",
        "batman",
        "damian",
        "drake",
        "gordon",
        "huntress",
        "lucius",
        "nightwing",
        "rasalghul",
        "robin",
    }
)
_ALFRED_LAUNCHD_LABEL_PHRASES = frozenset(
    {
        "agent-cleanup",
        "brand-mention-scanner",
        "code-map-refresh",
        "cold-backup",
        "content-drift",
        "fleet-brain",
        "fleet-doctor",
        "fleet-ingest",
        "fleet-recap",
        "memory-auto-promote",
        "memory-harvest",
        "morning-brief",
        "proof-telemetry",
        "shipped-summary",
        "slack-listener",
    }
)
_LAUNCHD_ORPHAN_SKIP_PREFIXES = ("application.", "com.apple.")
_LAUNCHCTL_PRINT_TIMEOUT_SECONDS = 0.25
_ALFRED_SCHEDULER_LAUNCHER_NAMES = frozenset({"agent-launch"})
_ENV_LEGACY_LAUNCHD_LABEL_PREFIXES = "ALFRED_SETUP_LEGACY_LAUNCHD_LABEL_PREFIXES"
_INFERRED_LEGACY_PREFIX_MIN_EVIDENCE = 2
_LAUNCHD_PROBE_UNAVAILABLE = "launchd probe unavailable"
_SYSTEMD_PROBE_UNAVAILABLE = "systemd probe unavailable"
_SYSTEMD_TIMER_LOOKUP_UNAVAILABLE = "systemd timer lookup unavailable"
_SCHEDULER_PROBE_UNAVAILABLE = frozenset(
    {
        _LAUNCHD_PROBE_UNAVAILABLE,
        _SYSTEMD_PROBE_UNAVAILABLE,
        _SYSTEMD_TIMER_LOOKUP_UNAVAILABLE,
    }
)
_LAUNCHD_UNREADABLE_SUFFIX = " (unreadable)"
_COMMON_REVERSE_DNS_PREFIXES = frozenset({"app", "com", "dev", "io", "net", "org"})

_DEMO_FILENAME = "setup-demo-cards.json"
# A made-up slug the demo cards live under. It is never a real ``owner/repo``,
# so a demo card can never be mistaken for (or acted on as) real fleet work.
DEMO_REPO = "alfred/demo"

_CAPABILITY_SOURCES: dict[str, dict[str, str]] = {
    "code_graph": {
        "source": "DeusData/codebase-memory-mcp",
        "url": "https://github.com/DeusData/codebase-memory-mcp",
        "license": "MIT",
    },
    "context_compression": {
        "source": "headroomlabs-ai/headroom",
        "url": "https://github.com/headroomlabs-ai/headroom",
        "license": "Apache-2.0",
    },
    "engineering_skills": {
        "source": "garrytan/gstack, vercel-labs/agent-skills, addyosmani/agent-skills",
        "url": "https://github.com/garrytan/gstack",
        "license": "MIT",
    },
}


def decode_env_value(value: str) -> str:
    """Decode one shell-style env-file value without importing agent_runner."""

    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("'\"'\"'", "'")
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


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


def _setup_config_value(key: str, default: str = "") -> str:
    return _runtime_config_value(key, default)


def _runtime_config_env() -> dict[str, str]:
    env = dict(os.environ)
    protected = {key for key, value in os.environ.items() if value.strip()}
    protected.update(key for key in _REPO_ENV_KEYS if key in os.environ)
    raw_home = env.get("ALFRED_HOME", "").strip()
    if raw_home:
        runtime_home = _safe_expand_path(raw_home) or Path(raw_home)
    else:
        runtime_home = _default_alfred_home(env)
        env["ALFRED_HOME"] = str(runtime_home)
    _load_launcher_env_file(runtime_home / ".env", env, protected_keys=protected)
    return env


_SLACK_CONFIG_KEYS = (
    "SLACK_WEBHOOK_URL",
    "SLACK_WEBHOOK_SECRET_ID",
    "SLACK_BOT_TOKEN",
    "ALFRED_SLACK_BOT_TOKEN_SECRET_ID",
    "SLACK_APP_TOKEN",
    "ALFRED_SLACK_APP_TOKEN",
)

_MEMORY_CONFIG_KEYS = (
    "ALFRED_REDIS_MEMORY_URL",
    "ALFRED_REDIS_MEMORY_NAMESPACE",
    "ALFRED_AMS_HOST",
    "ALFRED_AMS_PORT",
    "ALFRED_AMS_REDIS_URL",
)


def _runtime_config_value(key: str, default: str = "") -> str:
    return _runtime_config_env().get(key, "").strip() or default


def _queue_config_value(key: str, default: str = "") -> str:
    return _runtime_config_value(key, default)


def _allowed_queue_repos() -> set[str]:
    repos: set[str] = set()
    for key in _REPO_ENV_KEYS:
        raw = _queue_config_value(key)
        repos.update(normalize_repo_slugs(re.split(r"[\s,]+", raw)))
    return repos


def _gh_bin() -> str:
    return _setup_config_value("ALFRED_GH_BIN") or _setup_config_value("GH_BIN") or "gh"


def _gh_subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = _join_search_path(_engine_search_path(env), env.get("PATH", ""))
    return env


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


def selected_repos(env: dict[str, str] | None = None) -> list[str]:
    """The board-visible repos selected for first-run setup.

    Setup owns the board/bridge repo picker, not the narrower queue mutation
    scope. Reading only ``ALFRED_SHIPPED_REPOS`` / ``ALFRED_BRIDGE_REPOS`` keeps
    queue-only repos from being pre-checked and then accidentally written back as
    board-visible repos.
    """
    if env is not None:
        return sorted(_repos_from_env(env, _BOARD_REPO_ENV_KEYS))

    runtime_env = _runtime_config_env()
    repos = _repos_from_env(runtime_env, _BOARD_REPO_ENV_KEYS)
    if repos or any(_has_config_key(runtime_env, key) for key in _BOARD_REPO_ENV_KEYS):
        return sorted(repos)
    return []


def setup_board_repos(env: dict[str, str] | None = None) -> list[str]:
    """Repos that make the setup board usable.

    Queue-only scope is enough for hold/queue/done mutations, but the Home board
    scans ``ALFRED_SHIPPED_REPOS`` / ``ALFRED_BRIDGE_REPOS``. Setup readiness
    must therefore key off the board-visible repo knobs, not the broader queue
    allowlist.
    """
    resolved = env or _runtime_config_env()
    return sorted(_repos_from_env(resolved, _BOARD_REPO_ENV_KEYS))


# --------------------------------------------------------------------------- #
# .env writer
# --------------------------------------------------------------------------- #
def _env_path(env: dict[str, str] | None = None) -> Path:
    resolved = env if env is not None else dict(os.environ)
    return _alfred_home(resolved) / ".env"


def _alfred_home(env: dict[str, str] | None = None) -> Path:
    source = dict(os.environ) if env is None else env
    raw = str(source.get("ALFRED_HOME", "")).strip()
    if raw:
        path = _safe_expand_path(raw)
        if path:
            return path
        return Path(raw)
    return _default_alfred_home(source)


def _format_repo_value(repos: list[str]) -> str:
    return ",".join(repos)


def _repos_from_env(
    env: dict[str, str],
    keys: tuple[str, ...] = _REPO_ENV_KEYS,
) -> set[str]:
    repos: set[str] = set()
    for key in keys:
        raw = _code_memory_config(env, key)
        repos.update(normalize_repo_slugs(re.split(r"[\s,]+", raw)))
    return repos


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
    _write_env_file_values(path, values)
    return path


def _write_env_file_values(path: Path, values: dict[str, str], *, export: bool = False) -> None:
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
            raw_name = stripped.partition("=")[0].strip()
            name = raw_name.removeprefix("export ").strip()
            if name in remaining:
                prefix = "export " if export else ""
                out_lines.append(f"{prefix}{name}={remaining.pop(name)}")
                continue
        out_lines.append(line)
    for name, value in remaining.items():
        prefix = "export " if export else ""
        out_lines.append(f"{prefix}{name}={value}")

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


def persist_selected_repos(
    repos: list[str],
    *,
    queue_repos: list[str] | None = None,
    replace_queue_repos: bool = False,
) -> dict[str, Any]:
    """Persist the chosen repo allowlist and mirror it into the live process.

    Writes the board allowlist keys to ``.env`` and updates ``os.environ`` so
    the change takes effect for this running server without a restart
    (``config_value`` prefers the process env, and a fresh board call then sees
    the new scope immediately). Queue mutation scope is only written when the
    caller supplies ``queue_repos`` explicitly and there is no existing queue
    scope. Existing queue scopes are only replaced by ``replace_queue_repos``.
    """
    clean = normalize_repo_slugs(repos)
    clean_queue = normalize_repo_slugs(queue_repos) if queue_repos is not None else None
    values = _repo_scope_values_for_save(
        clean,
        queue_repos=clean_queue,
        replace_queue_repos=replace_queue_repos,
    )
    env_path = write_env_values(values)
    for key in values:
        # Mirror into the live process so the new scope is effective now. An
        # empty selection clears the override so the resolver falls back to the
        # .env value (also empty), which is the honest "nothing scoped" state.
        if values[key]:
            os.environ[key] = values[key]
        else:
            os.environ.pop(key, None)
    return {
        "repos": clean,
        "env_path": str(env_path),
        "keys": list(values),
    }


def _repo_scope_values_for_save(
    repos: list[str],
    *,
    queue_repos: list[str] | None = None,
    replace_queue_repos: bool = False,
) -> dict[str, str]:
    """Repo keys to persist for a setup repo save.

    The onboarding repo picker owns the board-visible scope. Queue scope is a
    mutation boundary, so guided saves can seed it on fresh installs but must
    preserve any existing queue scope. Replacing an existing queue allowlist
    requires ``replace_queue_repos`` so board visibility cannot widen mutation
    permissions as a side effect.
    """

    value = _format_repo_value(repos)
    values = {
        SHIPPED_REPOS_ENV: value,
        BRIDGE_REPOS_ENV: value,
    }
    runtime_env = _runtime_config_env()
    queue_scope_present, existing_queue = _effective_queue_scope_for_save(runtime_env)
    if queue_repos is not None and (replace_queue_repos or not queue_scope_present):
        return {QUEUE_REPOS_ENV: _format_repo_value(queue_repos), **values}

    if queue_scope_present:
        values = {QUEUE_REPOS_ENV: _format_repo_value(existing_queue), **values}
    return values


def _effective_queue_scope_for_save(runtime_env: dict[str, str]) -> tuple[bool, list[str]]:
    runtime_queue = _repos_from_env(runtime_env, (QUEUE_REPOS_ENV,))
    if runtime_queue or _has_config_key(runtime_env, QUEUE_REPOS_ENV):
        return True, sorted(runtime_queue)
    return False, []


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
    gh_env = _gh_subprocess_env()
    if shutil.which(gh, path=gh_env.get("PATH")) is None and not os.path.isabs(gh):
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
            env=gh_env,
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
    search = _join_search_path(_engine_search_path(os.environ), os.environ.get("PATH", ""))
    out: list[dict[str, Any]] = []
    for name in _ENGINE_BINS:
        configured = _setup_config_value(f"{name.upper()}_BIN")
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


def code_memory_status(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Detect the optional code-structure memory layer without mutating state.

    ``bin/code-memory-mcp doctor`` may auto-fetch the pinned upstream binary,
    which is great for an explicit repair action but too surprising for the
    read-only setup checklist. This probe only inspects config, PATH, the
    pinned launcher metadata, and the existing index directory.
    """

    launcher_env = env or _code_memory_launcher_env()
    enabled = _config_flag(launcher_env, "ALFRED_CODE_MEMORY_MCP", default=True)
    autofetch = _config_flag(launcher_env, "ALFRED_CODE_MEMORY_AUTOFETCH", default=True)
    binary = _code_memory_binary(launcher_env)
    index_dir = _code_memory_index_dir(launcher_env)
    index_home = _code_memory_home(launcher_env, index_dir)
    graph_dir = _code_memory_graph_dir(launcher_env, index_home)
    repo_scope = (
        _code_memory_repo_scope(launcher_env)
        if enabled
        else _disabled_code_memory_repo_scope(launcher_env)
    )
    index_present = _code_memory_index_present(graph_dir)
    pin = _code_memory_pin(launcher_env)

    if not enabled:
        detail = "Code memory is disabled with ALFRED_CODE_MEMORY_MCP."
    elif binary["resolved"] and index_present:
        detail = "Code-memory binary and index are present."
    elif binary["resolved"]:
        detail = "Code-memory binary is present; run an index before relying on graph queries."
    elif autofetch:
        detail = "Code-memory binary is not installed yet; Alfred can fetch the pinned release on first explicit use."
    else:
        detail = "Code-memory binary is not installed and autofetch is disabled."

    return {
        "enabled": enabled,
        "autofetch": autofetch,
        "binary": binary,
        "version_pin": pin["version"],
        "repo": pin["repo"],
        "index_dir": str(index_dir),
        "index_home": str(index_home),
        "graph_dir": str(graph_dir),
        "index_present": index_present,
        "repos": repo_scope,
        "detail": detail,
    }


def capability_status(
    code_memory: dict[str, Any] | None = None,
    launcher_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return Alfred's local capability plane without installing anything.

    The native setup flow needs one stable contract for "what makes this fleet
    enterprise-ready" instead of a scattering of bespoke probes. This detector
    stays read-only: it reports what is present, what Alfred can safely fetch on
    explicit use, and which optional packages are still missing.
    """

    runtime_env = launcher_env or _runtime_config_env()
    code_memory = code_memory or code_memory_status()
    capabilities = [
        _code_graph_capability(code_memory),
        _context_compression_capability(runtime_env),
        _engineering_skills_capability(runtime_env),
    ]
    counts = {
        "ready": sum(1 for item in capabilities if item["state"] == "ready"),
        "actionable": sum(
            1
            for item in capabilities
            if item["state"] in {"installable", "missing", "needs_index", "available"}
        ),
        "disabled": sum(1 for item in capabilities if item["state"] == "disabled"),
    }
    return {
        "version": 1,
        "summary": counts | {"total": len(capabilities)},
        "capabilities": capabilities,
    }


def _capability_base(
    key: str,
    *,
    title: str,
    category: str,
    recommended: bool,
    state: str,
    detail: str,
    installed: bool,
    enabled: bool,
    install_hint: str,
    detected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = _CAPABILITY_SOURCES[key]
    return {
        "key": key,
        "title": title,
        "category": category,
        "recommended": recommended,
        "state": state,
        "installed": installed,
        "enabled": enabled,
        "detail": detail,
        "detected": detected or {},
        "install_hint": install_hint,
        "source": source,
    }


def _code_graph_capability(code_memory: dict[str, Any]) -> dict[str, Any]:
    binary = code_memory.get("binary") or {}
    enabled = bool(code_memory.get("enabled"))
    installed = bool(binary.get("resolved"))
    indexed = bool(code_memory.get("index_present"))
    if not enabled:
        state = "disabled"
    elif installed and indexed:
        state = "ready"
    elif installed:
        state = "needs_index"
    elif code_memory.get("autofetch"):
        state = "installable"
    else:
        state = "missing"
    return _capability_base(
        "code_graph",
        title="Code graph memory",
        category="memory",
        recommended=True,
        state=state,
        installed=installed,
        enabled=enabled,
        detail=str(code_memory.get("detail") or ""),
        detected={
            "binary": binary,
            "index_dir": code_memory.get("index_dir"),
            "index_present": indexed,
            "repos": code_memory.get("repos"),
            "version_pin": code_memory.get("version_pin"),
        },
        install_hint="Run `alfred code-memory doctor`, then `alfred code-memory index`.",
    )


def _context_compression_capability(env: Mapping[str, str]) -> dict[str, Any]:
    search = _join_search_path(_engine_search_path(env), env.get("PATH", ""))
    binary = shutil.which("headroom", path=search)
    enabled = _env_flag(env, "ALFRED_CONTEXT_COMPRESSION", default=False)
    if binary:
        state = "available"
        detail = (
            "Headroom CLI is installed; Alfred will report ready after runner wiring is enabled."
            if enabled
            else "Headroom CLI is installed; runner integration is not wired yet."
        )
    else:
        state = "missing"
        detail = (
            "Headroom is not installed yet; Alfred can use it as a local token-compression layer."
        )
    return _capability_base(
        "context_compression",
        title="Context compression",
        category="tokens",
        recommended=True,
        state=state,
        installed=bool(binary),
        enabled=enabled,
        detail=detail,
        detected={"binary": binary, "env_key": "ALFRED_CONTEXT_COMPRESSION"},
        install_hint=(
            "Install `headroom-ai[all]` with pip or `headroom-ai` with npm, "
            "then run `headroom doctor`."
        ),
    )


def _engineering_skills_capability(env: Mapping[str, str]) -> dict[str, Any]:
    paths = _installed_skill_paths(env)
    installed = bool(paths)
    if installed:
        state = "ready"
        detail = "At least one engineering skill pack is installed for a local agent host."
    else:
        state = "missing"
        detail = (
            "No recommended engineering skill pack was found in Claude or Codex skill directories."
        )
    return _capability_base(
        "engineering_skills",
        title="Engineering skill packs",
        category="skills",
        recommended=True,
        state=state,
        installed=installed,
        enabled=installed,
        detail=detail,
        detected={"paths": [str(path) for path in paths]},
        install_hint=(
            "Install gstack and the Vercel/Addy agent-skill packs for review, QA, "
            "security, docs, and frontend workflows."
        ),
    )


def _env_flag(env: Mapping[str, str], key: str, *, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSEY


def _installed_skill_paths(env: Mapping[str, str]) -> list[Path]:
    roots = _skill_roots(env)
    patterns = (
        "gstack",
        "gstack-*",
        "agent-skills",
        "vercel-*",
        "react-best-practices",
        "web-design-guidelines",
        "frontend-ui-engineering",
    )
    out: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for pattern in patterns:
            for path in root.glob(pattern):
                if path.is_dir() and path not in seen:
                    seen.add(path)
                    out.append(path)
    return sorted(out, key=lambda p: str(p))


def _skill_roots(env: Mapping[str, str]) -> list[Path]:
    home = _safe_home(env)
    roots: list[Path] = []
    codex_home = env.get("CODEX_HOME", "").strip()
    claude_home = env.get("CLAUDE_HOME", "").strip()
    if codex_home:
        path = _safe_expand_path(codex_home)
        if path:
            roots.append(path / "skills")
    elif home:
        roots.append(home / ".codex" / "skills")
    if claude_home:
        path = _safe_expand_path(claude_home)
        if path:
            roots.append(path / "skills")
    elif home:
        roots.append(home / ".claude" / "skills")
    return roots


def _safe_home(env: Mapping[str, str]) -> Path | None:
    raw = env.get("HOME", "").strip()
    if raw:
        path = _safe_expand_path(raw)
        if path:
            return path
    try:
        return Path.home()
    except RuntimeError:
        return None


def _safe_expand_path(raw: str) -> Path | None:
    try:
        return Path(raw).expanduser()
    except RuntimeError:
        return None


def _default_alfred_home(env: Mapping[str, str]) -> Path:
    home = _safe_home(env)
    if home:
        return home / ".alfred"
    return Path(".alfred")


def _engine_search_path(env: Mapping[str, str]) -> tuple[str, ...]:
    paths: list[str] = []
    home = _safe_home(env)
    if home:
        paths.extend([str(home / ".local" / "bin"), str(home / ".claude" / "local")])
    paths.extend(["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin"])
    return tuple(paths)


def _join_search_path(paths: tuple[str, ...], inherited_path: str) -> str:
    parts = [part for part in paths if part]
    parts.extend(part for part in inherited_path.split(os.pathsep) if part)
    return os.pathsep.join(parts)


def _code_memory_launcher_env() -> dict[str, str]:
    """Return setup-visible code-memory config for the connected runtime."""

    env = _runtime_config_env()
    if not env.get("WORKSPACE_ROOT", "").strip():
        home = _safe_home(env)
        if home:
            env["WORKSPACE_ROOT"] = str(home / "code")
    return env


def _load_launcher_env_file(
    path: Path, env: dict[str, str], *, protected_keys: set[str] | None = None
) -> None:
    protected_keys = protected_keys or set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _ENV_KEY_RE.match(key):
            continue
        if key in protected_keys:
            continue
        decoded = decode_env_value(_strip_inline_comment(value).strip())
        home = _safe_home(env)
        if home:
            decoded = decoded.replace("${HOME}", str(home)).replace("$HOME", str(home))
        env[key] = decoded


def _code_memory_config(env: dict[str, str], key: str, default: str = "") -> str:
    return env.get(key, "").strip() or default


def _config_flag(env: dict[str, str], key: str, *, default: bool) -> bool:
    raw = _code_memory_config(env, key).lower()
    if raw == "":
        return default
    return raw not in _FALSEY


def _code_memory_index_dir(env: dict[str, str]) -> Path:
    raw = _code_memory_config(env, "ALFRED_CODE_MEMORY_INDEX_DIR")
    if raw.strip():
        path = _safe_expand_path(raw)
        if path:
            return path
        return Path(raw)
    return _alfred_home(env) / "state" / "code-memory"


def _code_memory_home(env: dict[str, str], index_dir: Path) -> Path:
    raw = _code_memory_config(env, "ALFRED_CODE_MEMORY_HOME")
    if raw.strip():
        path = _safe_expand_path(raw)
        if path:
            return path
        return Path(raw)
    return index_dir


def _code_memory_graph_dir(env: dict[str, str], index_home: Path) -> Path:
    upstream_cache = _code_memory_config(env, "CBM_CACHE_DIR")
    if upstream_cache:
        path = _safe_expand_path(upstream_cache)
        if path:
            return path
        return Path(upstream_cache)
    return index_home / ".cache" / _CODE_MEMORY_BIN_NAME


def _code_memory_repos(env: dict[str, str]) -> list[str]:
    raw = _code_memory_config(env, "ALFRED_CODE_MEMORY_REPOS") or _code_memory_config(
        env, "ALFRED_CODE_MAP_REPOS"
    )
    out: list[str] = []
    seen: set[str] = set()
    for piece in raw.split(","):
        name = "".join(piece.split())
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _code_memory_workspace_subdir(env: dict[str, str]) -> str:
    if "ALFRED_WORKSPACE_SUBDIR" in env:
        return env.get("ALFRED_WORKSPACE_SUBDIR", "").strip()
    if "WORKSPACE_SUBDIR" in env:
        return env.get("WORKSPACE_SUBDIR", "").strip()
    return "product"


def _code_memory_workspace(env: dict[str, str]) -> Path:
    root = _code_memory_workspace_root(env)
    subdir = _code_memory_workspace_subdir(env)
    return root / subdir if subdir else root


def _code_memory_workspace_root(env: dict[str, str]) -> Path:
    configured = _code_memory_config(env, "WORKSPACE_ROOT")
    if configured:
        path = _safe_expand_path(configured)
        if path:
            return path
        return Path(configured)
    home = env.get("HOME", "").strip()
    if home:
        path = _safe_expand_path(home)
        if path:
            return path / "code"
        return Path(home) / "code"
    try:
        return Path.home() / "code"
    except (OSError, RuntimeError):
        return Path.cwd() / ".alfred-code-memory-workspace-unavailable"


def _code_memory_discovery_limit(env: dict[str, str]) -> int:
    raw = _code_memory_config(env, "ALFRED_CODE_MEMORY_DISCOVERY_LIMIT")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _CODE_MEMORY_DISCOVERY_LIMIT
    return value if value > 0 else _CODE_MEMORY_DISCOVERY_LIMIT


def _discover_code_memory_repos(env: dict[str, str]) -> list[str]:
    workspace = _code_memory_workspace(env)
    limit = _code_memory_discovery_limit(env)
    found: list[str] = []
    if not workspace.is_dir():
        return found
    queue = [workspace]
    seen_real_paths: set[Path] = set()
    while queue:
        repo = queue.pop(0)
        try:
            real_repo = repo.resolve(strict=False)
        except OSError:
            real_repo = repo.absolute()
        if real_repo in seen_real_paths:
            continue
        seen_real_paths.add(real_repo)
        try:
            entries = list(repo.iterdir())
        except OSError:
            continue
        if _is_code_memory_git_repo(repo):
            try:
                relative_parts = repo.relative_to(workspace).parts
            except ValueError:
                continue
            if any(part in _CODE_MEMORY_DISCOVERY_IGNORES for part in relative_parts):
                continue
            found.append(str(repo.relative_to(workspace)))
            if len(found) >= limit:
                break
            continue
        children = sorted(
            entry
            for entry in entries
            if entry.is_dir() and entry.name not in _CODE_MEMORY_DISCOVERY_IGNORES
        )
        for child in children:
            try:
                relative_parts = child.relative_to(workspace).parts
            except ValueError:
                continue
            if any(part in _CODE_MEMORY_DISCOVERY_IGNORES for part in relative_parts):
                continue
            queue.append(child)
    return found


def _existing_code_memory_configured_repos(env: dict[str, str], configured: list[str]) -> list[str]:
    workspace = _code_memory_workspace(env)
    return [name for name in configured if _is_code_memory_git_repo(workspace / name)]


def _is_code_memory_git_repo(path: Path) -> bool:
    try:
        return path.is_dir() and (path / ".git").exists()
    except OSError:
        return False


def _code_memory_repo_scope(env: dict[str, str]) -> dict[str, Any]:
    configured = _code_memory_repos(env)
    configured_existing = _existing_code_memory_configured_repos(env, configured)
    discovered: list[str] = [] if configured_existing else _discover_code_memory_repos(env)
    selected = configured_existing or discovered
    if configured_existing:
        source = "configured"
    elif configured:
        source = "auto-fallback"
    else:
        source = "auto"
    return {
        "configured": configured,
        "configured_existing": configured_existing,
        "discovered": discovered,
        "selected": selected,
        "source": source,
        "count": len(selected),
        "limit": _code_memory_discovery_limit(env),
    }


def _disabled_code_memory_repo_scope(env: dict[str, str]) -> dict[str, Any]:
    configured = _code_memory_repos(env)
    return {
        "configured": configured,
        "configured_existing": [],
        "discovered": [],
        "selected": configured,
        "source": "configured" if configured else "disabled",
        "count": len(configured),
        "limit": _code_memory_discovery_limit(env),
    }


def _code_memory_binary(env: dict[str, str]) -> dict[str, Any]:
    search = _join_search_path(_engine_search_path(env), env.get("PATH", ""))
    explicit = _code_memory_config(env, "ALFRED_CODE_MEMORY_BIN")
    if explicit:
        resolved = _resolve_configured_binary(explicit, search=search)
        if resolved:
            return {
                "resolved": True,
                "path": resolved,
                "source": "env",
                "configured": explicit,
            }

    on_path = shutil.which(_CODE_MEMORY_BIN_NAME, path=search)
    if on_path:
        return {"resolved": True, "path": on_path, "source": "path", "configured": explicit or None}

    cache_bin = _alfred_home(env) / "bin" / _CODE_MEMORY_BIN_NAME
    if cache_bin.is_file() and os.access(cache_bin, os.X_OK):
        return {
            "resolved": True,
            "path": str(cache_bin),
            "source": "cache",
            "configured": explicit or None,
        }

    return {
        "resolved": False,
        "path": None,
        "source": "env" if explicit else "none",
        "configured": explicit or None,
    }


def _resolve_configured_binary(value: str, *, search: str) -> str | None:
    path = _safe_expand_path(value) or Path(value)
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    found = shutil.which(value, path=search)
    return found or None


def _code_memory_pin(env: dict[str, str]) -> dict[str, str]:
    version = _code_memory_config(env, "ALFRED_CODE_MEMORY_VERSION")
    repo = _code_memory_config(env, "ALFRED_CODE_MEMORY_REPO")
    try:
        for line in _CODE_MEMORY_LAUNCHER.read_text(encoding="utf-8").splitlines():
            if not version:
                match = _CODE_MEMORY_VERSION_RE.match(line)
                if match:
                    version = match.group(1)
                    continue
            if not repo:
                match = _CODE_MEMORY_REPO_RE.match(line)
                if match:
                    repo = match.group(1)
    except OSError:
        pass
    return {
        "version": version or "unknown",
        "repo": repo or "DeusData/codebase-memory-mcp",
    }


def _code_memory_index_present(graph_dir: Path) -> bool:
    return _has_graph_artifact(graph_dir)


def _has_graph_artifact(path: Path) -> bool:
    try:
        if path.is_file():
            return path.suffix.lower() in _CODE_MEMORY_GRAPH_SUFFIXES
        if not path.is_dir():
            return False
        for child in path.rglob("*"):
            if child.is_file() and child.suffix.lower() in _CODE_MEMORY_GRAPH_SUFFIXES:
                return True
    except OSError:
        return False
    return False


def bootstrap_status() -> dict[str, Any]:
    """One read the client turns into the Set up checklist.

    Surfaces what is connected vs missing with a next action per row:
    GitHub auth, at least one engine CLI, the watched-repo selection, and a
    demo-present flag. ``ready`` is the golden-path gate: gh authed + at least
    one engine + at least one board-visible repo selected and covered by queue
    scope (no AWS / Slack required).
    """
    gh = gh_auth_status()
    engines = engine_clis()
    runtime_env = _runtime_config_env()
    repos = setup_board_repos(runtime_env)
    queue_repos = _setup_queue_repos_for_status(runtime_env)
    queue_missing = sorted(set(repos) - queue_repos)
    queue_covers_selected = bool(repos) and not queue_missing
    any_engine = any(e["installed"] for e in engines)
    code_memory = code_memory_status(runtime_env)
    capability_plane = capability_status(code_memory, launcher_env=runtime_env)
    return {
        "github": gh,
        "engines": engines,
        "engine_ready": any_engine,
        "code_memory": code_memory,
        "capability_plane": capability_plane,
        "repos": {
            "selected": repos,
            "count": len(repos),
            "keys": list(_REPO_ENV_KEYS),
        },
        "queue": {
            "ready": bool(queue_repos),
            "count": len(queue_repos),
            "covers_selected": queue_covers_selected,
            "missing_selected": queue_missing,
        },
        "demo": {"present": any(load_demo_cards().values())},
        "install": install_inventory(repos=repos, env=runtime_env),
        "ready": bool(gh["ok"] and any_engine and repos and queue_repos and queue_covers_selected),
    }


def _setup_queue_repos_for_status(env: dict[str, str]) -> set[str]:
    return _repos_from_env(env, (QUEUE_REPOS_ENV,))


def install_inventory(
    *,
    repos: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Read-only inventory of an existing Alfred install.

    The desktop onboarding uses this to show what Alfred already found on the
    machine. It deliberately exposes only paths, booleans, counts, and
    plain-language detail, never secret values from ``.env`` or the server token.
    """

    from . import schedule as setup_schedule

    resolved_env = env or _runtime_config_env()
    home = _alfred_home(resolved_env)
    env_path = home / ".env"
    token_path = home / "state" / "server-token"
    conf_path = _install_agents_conf_path(home)
    scheduled_runs = setup_schedule.upcoming_runs(conf_path=conf_path) if conf_path else []
    unmanaged_scheduler_jobs = _unmanaged_alfred_scheduler_jobs(resolved_env, home)
    selected = repos if repos is not None else setup_board_repos(resolved_env)
    selected_env_present = any(_has_config_key(resolved_env, key) for key in _REPO_ENV_KEYS)
    board_env_present = any(_has_config_key(resolved_env, key) for key in _BOARD_REPO_ENV_KEYS)
    slack_configured = any(_has_config_value(resolved_env, key) for key in _SLACK_CONFIG_KEYS)
    memory_overridden = any(_has_config_value(resolved_env, key) for key in _MEMORY_CONFIG_KEYS)
    memory_detail = (
        "Custom Redis Agent Memory settings found."
        if memory_overridden
        else "Using bundled local Redis Agent Memory defaults."
    )

    items = [
        _inventory_item(
            "home",
            "Runtime home",
            home.exists(),
            f"{'Found' if home.exists() else 'Will create'} {home}",
            home,
        ),
        _inventory_item(
            "env",
            "Configuration file",
            env_path.is_file(),
            f"{'Found' if env_path.is_file() else 'Not created yet'} {env_path}",
            env_path,
        ),
        _inventory_item(
            "agents",
            "Scheduled fleet",
            bool(conf_path and conf_path.is_file()),
            (
                f"{len(scheduled_runs)} enabled scheduled run"
                f"{'' if len(scheduled_runs) == 1 else 's'} in agents.conf"
                if conf_path and conf_path.is_file()
                else "No deployed agents.conf found yet"
            ),
            conf_path,
        ),
        _inventory_item(
            "scheduler_unmanaged",
            "Unmanaged scheduler jobs",
            not _unmanaged_scheduler_jobs_block_setup(unmanaged_scheduler_jobs),
            _unmanaged_scheduler_detail(
                unmanaged_scheduler_jobs,
                scheduler_kind=_scheduler_probe_kind(resolved_env),
            ),
            (
                _scheduler_inventory_path(resolved_env)
                if _unmanaged_scheduler_jobs_block_setup(unmanaged_scheduler_jobs)
                else None
            ),
        ),
        _inventory_item(
            "repos",
            "Repository scope",
            bool(selected),
            (
                f"{len(selected)} board-visible repos in {', '.join(_BOARD_REPO_ENV_KEYS)}"
                if selected
                else (
                    "Queue-only repo scope found; save repositories to wire the board."
                    if selected_env_present and not board_env_present
                    else "No repositories selected yet"
                )
            ),
            env_path if selected_env_present else None,
        ),
        _inventory_item(
            "slack",
            "Slack approvals",
            slack_configured,
            (
                "Slack webhook or app tokens are configured."
                if slack_configured
                else "Optional. Not configured yet."
            ),
            env_path if slack_configured else None,
            optional=True,
        ),
        _inventory_item(
            "memory",
            "Memory layer",
            True,
            memory_detail,
            env_path if memory_overridden else None,
        ),
        _inventory_item(
            "token",
            "Desktop mutation token",
            token_path.is_file(),
            (
                "Runtime token is present for desktop actions."
                if token_path.is_file()
                else "Start the runtime to create the desktop action token."
            ),
            token_path.parent,
        ),
    ]

    initialized = any(
        (
            home.exists(),
            env_path.is_file(),
            bool(conf_path and conf_path.is_file()),
            bool(selected),
            token_path.is_file(),
            _unmanaged_scheduler_jobs_indicate_install(unmanaged_scheduler_jobs),
        )
    )
    return {
        "alfred_home": str(home),
        "env_path": str(env_path),
        "env_present": env_path.is_file(),
        "server_token_present": token_path.is_file(),
        "agents_conf_path": str(conf_path) if conf_path else None,
        "agents_conf_present": bool(conf_path and conf_path.is_file()),
        "scheduled_runs": len(scheduled_runs),
        "unmanaged_scheduler_jobs": unmanaged_scheduler_jobs,
        "unmanaged_scheduler_count": len(unmanaged_scheduler_jobs),
        "selected_repos_env_present": selected_env_present,
        "slack_configured": slack_configured,
        "memory_configured": memory_overridden,
        "initialized": initialized,
        "items": items,
    }


def _inventory_item(
    key: str,
    label: str,
    ok: bool,
    detail: str,
    path: Path | None = None,
    *,
    optional: bool = False,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "ok": bool(ok),
        "detail": detail,
        "path": str(path) if path else None,
        "optional": optional,
    }


def _unmanaged_scheduler_jobs_indicate_install(labels: list[str]) -> bool:
    return bool(labels) and any(label not in _SCHEDULER_PROBE_UNAVAILABLE for label in labels)


def _unmanaged_scheduler_jobs_block_setup(labels: list[str]) -> bool:
    return bool(labels) and labels != [_SYSTEMD_TIMER_LOOKUP_UNAVAILABLE]


def _install_agents_conf_path(home: Path) -> Path | None:
    for conf in (
        home / "launchd" / "agents.conf",
        home / "infra" / "agents" / "launchd" / "agents.conf",
    ):
        if conf.is_file():
            return conf
    return None


def _launch_agents_dir(env: Mapping[str, str]) -> Path | None:
    home = _safe_home(env)
    if home is None:
        return None
    return home / "Library" / "LaunchAgents"


def _systemd_user_dir(env: Mapping[str, str]) -> Path | None:
    configured = env.get("ALFRED_SYSTEMD_USER_DIR", "").strip()
    if configured:
        return _safe_expand_path(configured) or Path(configured)
    home = _safe_home(env)
    if home is None:
        return None
    return home / ".config" / "systemd" / "user"


def _scheduler_inventory_path(env: Mapping[str, str]) -> Path | None:
    if _scheduler_probe_kind(env) == "systemd":
        return _systemd_user_dir(env)
    return _launch_agents_dir(env)


def _managed_launchd_labels(home: Path) -> set[str]:
    return _agents_conf_launchd_labels(_install_agents_conf_path(home))


def _agents_conf_launchd_labels(path: Path | None) -> set[str]:
    if path is None:
        return set()
    with suppress(OSError):
        labels: set[str] = set()
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            label = raw_line.split("\t", 1)[0].strip()
            if label:
                labels.add(label)
        return labels
    return set()


def _unmanaged_alfred_scheduler_jobs(env: Mapping[str, str], home: Path) -> list[str]:
    if _scheduler_probe_kind(env) == "systemd":
        return _unmanaged_alfred_systemd_jobs(env, home)
    return _unmanaged_alfred_launchd_jobs(env, home)


def _scheduler_probe_kind(env: Mapping[str, str]) -> str:
    if env.get("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "").strip():
        return "launchd"
    if env.get("ALFRED_SETUP_SYSTEMD_LIST_FIXTURE", "").strip():
        return "systemd"
    return "systemd" if os.uname().sysname == "Linux" else "launchd"


def _unmanaged_alfred_launchd_jobs(env: Mapping[str, str], home: Path) -> list[str]:
    launch_agents = _launch_agents_dir(env)
    if launch_agents is None:
        return []
    managed = _managed_launchd_labels(home)
    loaded = _loaded_launchd_labels(env)
    if loaded is None:
        return [_LAUNCHD_PROBE_UNAVAILABLE]
    if not loaded:
        return []
    legacy_prefixes = _legacy_launchd_label_prefixes(env, loaded)
    labels: list[str] = []
    checked_labels: set[str] = set()
    unreadable_labels: set[str] = set()
    if launch_agents.is_dir():
        for plist in launch_agents.glob("*.plist"):
            label, plist_args = _launchd_plist_identity(plist)
            if not label or label in managed or label not in loaded:
                continue
            active_args = _launchctl_program_args(label, env)
            if active_args is None:
                if _is_current_auxiliary_launchd_job(plist_args):
                    unreadable_labels.add(label)
                    continue
                if _strong_unreadable_alfred_scheduler_label(
                    label, legacy_prefixes
                ) or _program_is_alfred_scheduler(plist_args, home, label, legacy_prefixes):
                    unreadable_labels.add(label)
                continue
            checked_labels.add(label)
            program_args = active_args
            if _is_current_auxiliary_launchd_job(program_args):
                continue
            if _program_is_alfred_scheduler(program_args, home, label, legacy_prefixes):
                labels.append(label)
    for label in _loaded_launchd_labels_to_probe(loaded, managed, legacy_prefixes):
        if label in checked_labels:
            continue
        program_args = _launchctl_program_args(label, env)
        if program_args is None:
            if label in unreadable_labels or _strong_unreadable_alfred_scheduler_label(
                label, legacy_prefixes
            ):
                labels.append(_unreadable_launchd_label(label))
            continue
        if _is_current_auxiliary_launchd_job(program_args):
            continue
        if _program_is_alfred_scheduler(program_args, home, label, legacy_prefixes):
            labels.append(label)
    return sorted(set(labels))


def _unmanaged_alfred_systemd_jobs(env: Mapping[str, str], home: Path) -> list[str]:
    systemd_user_dir = _systemd_user_dir(env)
    if systemd_user_dir is None:
        return []
    managed = _managed_launchd_labels(home)
    loaded = _loaded_systemd_timer_labels(env)
    if loaded is None:
        return [_SYSTEMD_PROBE_UNAVAILABLE]
    if not loaded:
        return []
    legacy_prefixes = _legacy_launchd_label_prefixes(env, loaded)
    labels: list[str] = []
    probe_unavailable = False
    for label in sorted(loaded):
        if label in managed:
            continue
        service_label, service_lookup_failed = _systemd_timer_service_label(label, env)
        if service_lookup_failed:
            if _strong_unreadable_alfred_systemd_timer_label(label, legacy_prefixes):
                labels.append(_unreadable_launchd_label(label))
                continue
            service_label, timer_file_found = _systemd_timer_file_service_label(label, env)
            if not timer_file_found:
                probe_unavailable = True
                continue
            if service_label is None:
                continue
        elif service_label is None:
            continue
        program_args = _systemd_service_program_args(service_label, env, allow_disk_fallback=False)
        if program_args is None:
            if _strong_unreadable_alfred_scheduler_label(
                label,
                legacy_prefixes,
            ) or _strong_unreadable_alfred_scheduler_label(service_label, legacy_prefixes):
                labels.append(_unreadable_launchd_label(label))
            continue
        if _program_is_alfred_scheduler_for_labels(
            program_args,
            home,
            (label, service_label),
            legacy_prefixes,
        ):
            labels.append(label)
    if labels:
        return sorted(set(labels))
    return [_SYSTEMD_TIMER_LOOKUP_UNAVAILABLE] if probe_unavailable else []


def _loaded_launchd_labels(env: Mapping[str, str]) -> set[str] | None:
    fixture = env.get("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "").strip()
    if fixture:
        return {line.strip() for line in fixture.splitlines() if line.strip()}
    if os.uname().sysname != "Darwin":
        return set()
    try:
        cp = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            env=_scheduler_probe_subprocess_env(env),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    labels: set[str] = set()
    for raw in (cp.stdout or "").splitlines():
        parts = raw.split()
        if len(parts) >= 3 and parts[-1] != "Label":
            labels.add(parts[-1])
    return labels


def _loaded_systemd_timer_labels(env: Mapping[str, str]) -> set[str] | None:
    fixture = env.get("ALFRED_SETUP_SYSTEMD_LIST_FIXTURE", "").strip()
    if fixture:
        labels: set[str] = set()
        for raw in fixture.splitlines():
            unit = raw.split(maxsplit=1)[0] if raw.split() else ""
            if unit.endswith(".timer"):
                labels.add(unit.removesuffix(".timer"))
        return labels
    if os.uname().sysname != "Linux":
        return set()
    try:
        cp = subprocess.run(
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=timer",
                "--state=active",
                "--no-legend",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            env=_scheduler_probe_subprocess_env(env),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    labels: set[str] = set()
    for raw in (cp.stdout or "").splitlines():
        unit = raw.split(maxsplit=1)[0] if raw.split() else ""
        if unit.endswith(".timer"):
            labels.add(unit.removesuffix(".timer"))
    return labels


def _loaded_launchd_labels_to_probe(
    loaded: set[str],
    managed: set[str],
    legacy_prefixes: tuple[str, ...] = (),
) -> list[str]:
    candidates = [
        label for label in loaded if label not in managed and _is_probeable_launchd_label(label)
    ]
    candidates.sort(
        key=lambda label: (not _looks_like_alfred_launchd_label(label, legacy_prefixes), label)
    )
    return candidates


def _is_probeable_launchd_label(label: str) -> bool:
    stripped = label.strip()
    if not stripped or any(char.isspace() for char in stripped):
        return False
    normalized = stripped.lower()
    return not normalized.startswith(_LAUNCHD_ORPHAN_SKIP_PREFIXES)


def _legacy_launchd_label_prefixes(
    env: Mapping[str, str], loaded: Iterable[str]
) -> tuple[str, ...]:
    prefixes: set[str] = set()
    prefixes.update(_configured_legacy_launchd_label_prefixes(env))
    prefixes.update(_inferred_legacy_launchd_label_prefixes(loaded))
    return tuple(sorted(prefixes))


def _configured_legacy_launchd_label_prefixes(env: Mapping[str, str]) -> tuple[str, ...]:
    prefixes: list[str] = []
    for raw in (env.get(_ENV_LEGACY_LAUNCHD_LABEL_PREFIXES) or "").split(","):
        prefix = raw.strip().lower()
        if not prefix or any(char.isspace() for char in prefix):
            continue
        prefixes.append(prefix if prefix.endswith(".") else f"{prefix}.")
    return tuple(prefixes)


def _inferred_legacy_launchd_label_prefixes(labels: Iterable[str]) -> tuple[str, ...]:
    evidence_counts: dict[str, int] = {}
    for label in labels:
        normalized = label.strip().lower()
        parts = normalized.split(".")
        if len(parts) < 3 or not _has_alfred_prefix_inference_evidence(normalized):
            continue
        prefix = ".".join(parts[:-1]) + "."
        evidence_counts[prefix] = evidence_counts.get(prefix, 0) + 1
    return tuple(
        sorted(
            prefix
            for prefix, count in evidence_counts.items()
            if count >= _INFERRED_LEGACY_PREFIX_MIN_EVIDENCE
        )
    )


def _looks_like_alfred_launchd_label(label: str, legacy_prefixes: tuple[str, ...] = ()) -> bool:
    normalized = label.strip().lower()
    if not normalized:
        return False
    if legacy_prefixes and any(normalized.startswith(prefix) for prefix in legacy_prefixes):
        return True
    return _base_looks_like_alfred_launchd_label(normalized)


def _base_looks_like_alfred_launchd_label(normalized: str) -> bool:
    if any(phrase in normalized for phrase in _ALFRED_LAUNCHD_LABEL_PHRASES):
        return True
    tokens = set(re.split(r"[^a-z0-9]+", normalized))
    return bool(tokens.intersection(_ALFRED_LAUNCHD_IDENTITY_TOKENS))


def _strong_alfred_scheduler_label(label: str, legacy_prefixes: tuple[str, ...] = ()) -> bool:
    normalized = label.strip().lower()
    if not normalized:
        return False
    if _label_matches_legacy_prefix(normalized, legacy_prefixes):
        return True
    if normalized.startswith(("alfred.", "alfred-", "old.alfred", "old-alfred")):
        return True
    return any(phrase in normalized for phrase in _ALFRED_LAUNCHD_LABEL_PHRASES)


def _strong_unreadable_alfred_scheduler_label(
    label: str,
    legacy_prefixes: tuple[str, ...] = (),
) -> bool:
    normalized = label.strip().lower()
    if not normalized:
        return False
    if _label_matches_legacy_prefix(normalized, legacy_prefixes):
        return True
    return normalized.startswith(("alfred.", "alfred-", "old.alfred", "old-alfred"))


def _strong_unreadable_alfred_systemd_timer_label(
    label: str,
    legacy_prefixes: tuple[str, ...] = (),
) -> bool:
    normalized = label.strip().lower()
    if _strong_alfred_scheduler_label(normalized, legacy_prefixes):
        return True
    return _strong_unreadable_alfred_scheduler_label(normalized, legacy_prefixes)


def _label_matches_legacy_prefix(
    normalized_label: str,
    legacy_prefixes: tuple[str, ...],
) -> bool:
    return bool(legacy_prefixes) and any(
        normalized_label.startswith(prefix) for prefix in legacy_prefixes
    )


def _has_alfred_prefix_inference_evidence(normalized: str) -> bool:
    if _base_looks_like_alfred_launchd_label(normalized):
        return True
    tokens = set(re.split(r"[^a-z0-9]+", normalized))
    return bool(tokens.intersection(_ALFRED_LAUNCHD_ROLE_TOKENS))


def _launchctl_program_args(label: str, env: Mapping[str, str]) -> list[str] | None:
    if os.uname().sysname != "Darwin":
        return None
    try:
        cp = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=_LAUNCHCTL_PRINT_TIMEOUT_SECONDS,
            check=False,
            env=_scheduler_probe_subprocess_env(env),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    lines = (cp.stdout or "").splitlines()
    program: str | None = None
    for index, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith("program = "):
            value = line.removeprefix("program = ").strip()
            if value:
                program = value
            continue
        if line == "arguments = {":
            args: list[str] = []
            for arg_raw in lines[index + 1 :]:
                arg = arg_raw.strip()
                if arg == "}":
                    break
                if "=>" in arg:
                    arg = arg.split("=>", 1)[1].strip()
                if arg:
                    args.append(arg)
            if args:
                return args
    return [program] if program else None


def _systemd_service_program_args(
    label: str,
    env: Mapping[str, str],
    *,
    allow_disk_fallback: bool = True,
) -> list[str] | None:
    active = _active_systemd_service_program_args(label, env)
    if active is not None:
        return active
    if not allow_disk_fallback:
        return None
    systemd_user_dir = _systemd_user_dir(env)
    if systemd_user_dir is not None:
        service = systemd_user_dir / f"{label}.service"
        with suppress(OSError):
            parsed = _systemd_execstart_program_args(service.read_text(encoding="utf-8"), env)
            if parsed is not None:
                return parsed
    return None


def _systemd_timer_service_label(
    label: str,
    env: Mapping[str, str],
) -> tuple[str | None, bool]:
    if os.uname().sysname != "Linux":
        return label, False
    try:
        cp = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                f"{label}.timer",
                "--property=Unit",
                "--value",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            env=_scheduler_probe_subprocess_env(env),
        )
    except (OSError, subprocess.SubprocessError):
        return None, True
    if cp.returncode != 0:
        return None, True
    unit = (cp.stdout or "").strip()
    if not unit:
        return label, False
    if not unit.endswith(".service"):
        return None, False
    return unit.removesuffix(".service"), False


def _systemd_timer_file_service_label(
    label: str, env: Mapping[str, str]
) -> tuple[str | None, bool]:
    systemd_user_dir = _systemd_user_dir(env)
    if systemd_user_dir is None:
        return None, False
    timer = systemd_user_dir / f"{label}.timer"
    with suppress(OSError):
        for raw in timer.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line.startswith("Unit="):
                continue
            unit = line.removeprefix("Unit=").strip()
            if not unit:
                return label, True
            if not unit.endswith(".service"):
                return None, True
            return unit.removesuffix(".service"), True
        return label, True
    return None, False


def _active_systemd_service_program_args(label: str, env: Mapping[str, str]) -> list[str] | None:
    if os.uname().sysname != "Linux":
        return None
    try:
        cp = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                f"{label}.service",
                "--property=ExecStart",
                "--value",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            env=_scheduler_probe_subprocess_env(env),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    value = (cp.stdout or "").strip()
    if not value:
        return None
    return _systemd_execstart_value_program_args(value, env)


def _systemd_execstart_program_args(text: str, env: Mapping[str, str]) -> list[str] | None:
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("ExecStart="):
            continue
        value = line.removeprefix("ExecStart=").strip()
        if not value:
            continue
        return _systemd_execstart_value_program_args(value, env)
    return None


def _systemd_execstart_value_program_args(value: str, env: Mapping[str, str]) -> list[str] | None:
    value = value.strip()
    if not value:
        return None
    argv_marker = "argv[]="
    if value.startswith("{") and "path=" in value:
        structured = _structured_systemd_execstart_program_args(value, env)
        if structured is not None:
            return structured
    if argv_marker in value:
        value = _systemd_structured_field_value(value, argv_marker) or ""
    value = _expand_systemd_home_specifier(value, env)
    with suppress(ValueError):
        args = shlex.split(value)
        if args:
            return _strip_systemd_exec_prefixes(args)
    return [value] if value else None


def _structured_systemd_execstart_program_args(
    value: str,
    env: Mapping[str, str],
) -> list[str] | None:
    path_value = _systemd_structured_field_value(value, "path=") or ""
    path_args = _split_systemd_execstart_value(path_value, env)
    if not path_args:
        return None
    executable = path_args[0]

    argv_value = _systemd_structured_field_value(value, "argv[]=") or ""
    argv_args = _split_systemd_execstart_value(argv_value, env)
    if not argv_args:
        return [executable]
    return [executable, *argv_args[1:]]


def _split_systemd_execstart_value(value: str, env: Mapping[str, str]) -> list[str]:
    expanded = _expand_systemd_home_specifier(value.strip(), env)
    if not expanded:
        return []
    with suppress(ValueError):
        return _strip_systemd_exec_prefixes(shlex.split(expanded))
    return _strip_systemd_exec_prefixes([expanded])


def _strip_systemd_exec_prefixes(args: list[str]) -> list[str]:
    if not args:
        return args
    first = args[0].lstrip("@-:+!")
    return ([first] if first else []) + args[1:]


def _systemd_structured_field_value(text: str, marker: str) -> str | None:
    start = text.find(marker)
    if start < 0:
        return None
    index = start + len(marker)
    quote: str | None = None
    escaped = False
    chars: list[str] = []
    while index < len(text):
        if quote is None and text[index] == "}":
            break
        if quote is None and text.startswith(" ; ", index):
            rest = text[index + 3 :]
            field = rest.split("=", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\\[\\])?", field or ""):
                break
        char = text[index]
        chars.append(char)
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = quote is not None
        elif char in {"'", '"'}:
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        index += 1
    value = "".join(chars).strip()
    return value or None


def _scheduler_probe_subprocess_env(env: Mapping[str, str]) -> dict[str, str]:
    probe_env = dict(os.environ)
    probe_env["PATH"] = _join_search_path(_engine_search_path(env), os.environ.get("PATH", ""))
    return probe_env


def _expand_systemd_home_specifier(value: str, env: Mapping[str, str]) -> str:
    if "%h" not in value:
        return value
    home = _safe_home(env)
    return value.replace("%h", str(home)) if home else value


def _launchd_plist_identity(path: Path) -> tuple[str, list[str]]:
    try:
        data = plistlib.loads(path.read_bytes())
    except Exception:
        return path.stem, []
    if not isinstance(data, dict):
        return path.stem, []
    raw_args = data.get("ProgramArguments")
    args = [str(arg) for arg in raw_args] if isinstance(raw_args, list) else []
    if not args:
        raw_program = data.get("Program")
        if isinstance(raw_program, str) and raw_program.strip():
            args = [raw_program.strip()]
    label = data.get("Label")
    return (str(label).strip() if label else path.stem), args


def _is_current_auxiliary_launchd_job(program_args: list[str]) -> bool:
    first = program_args[0] if program_args else ""
    return first.endswith("/ams-launch.sh")


def _program_runs_from_alfred_home(program_args: list[str], home: Path) -> bool:
    for path in _program_argument_paths(program_args):
        with suppress(ValueError):
            path.relative_to(home / "bin")
            return True
    return False


def _program_is_alfred_scheduler(
    program_args: list[str],
    home: Path,
    label: str,
    legacy_prefixes: tuple[str, ...] = (),
) -> bool:
    if _program_runs_from_alfred_home(program_args, home):
        return True
    looks_like_alfred_label = _strong_alfred_scheduler_label(label, legacy_prefixes)
    argument_paths = _program_argument_paths(program_args)
    if _label_matches_legacy_prefix(label.strip().lower(), legacy_prefixes) and any(
        path.name in _ALFRED_SCHEDULER_LAUNCHER_NAMES and path.parent.name == "bin"
        for path in argument_paths
    ):
        return True
    if _label_has_legacy_engineering_shape(label.strip().lower()) and any(
        _path_is_scheduler_launcher(path) for path in argument_paths
    ):
        return True
    if any(_path_is_external_alfred_scheduler_launcher(path) for path in argument_paths):
        return looks_like_alfred_label
    return False


def _program_is_alfred_scheduler_for_labels(
    program_args: list[str],
    home: Path,
    labels: Iterable[str],
    legacy_prefixes: tuple[str, ...] = (),
) -> bool:
    return any(
        _program_is_alfred_scheduler(program_args, home, label, legacy_prefixes) for label in labels
    )


def _path_is_external_alfred_scheduler_launcher(path: Path) -> bool:
    if not _path_is_scheduler_launcher(path):
        return False
    return any(_path_part_is_alfred_install_name(part) for part in path.parts[:-2])


def _path_is_scheduler_launcher(path: Path) -> bool:
    return path.name in _ALFRED_SCHEDULER_LAUNCHER_NAMES and path.parent.name == "bin"


def _label_has_legacy_engineering_shape(normalized_label: str) -> bool:
    parts = normalized_label.split(".")
    return len(parts) >= 3 and parts[1] == "eng" and parts[0] not in _COMMON_REVERSE_DNS_PREFIXES


def _path_part_is_alfred_install_name(part: str) -> bool:
    normalized = part.lower()
    return (
        normalized in {"alfred", "alfred-os"}
        or normalized.endswith("-alfred")
        or normalized.endswith("_alfred")
    )


def _program_argument_paths(program_args: list[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for raw in program_args:
        for token in _program_argument_path_tokens(raw):
            path = _safe_expand_path(token)
            if path is None:
                continue
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
    return paths


def _program_argument_path_tokens(raw: str) -> Iterable[str]:
    yield raw
    if not any(char.isspace() for char in raw):
        return
    with suppress(ValueError):
        for token in shlex.split(raw):
            yield token.rstrip(";&|")


def _unreadable_launchd_label(label: str) -> str:
    return f"{label}{_LAUNCHD_UNREADABLE_SUFFIX}"


def _unmanaged_scheduler_detail(labels: list[str], *, scheduler_kind: str = "launchd") -> str:
    if not labels:
        return "No unmanaged Alfred scheduler jobs found."
    if labels == [_LAUNCHD_PROBE_UNAVAILABLE]:
        return (
            "Could not query launchd for unmanaged Alfred jobs. Retry setup or inspect "
            "launchctl before switching this host to OSS scheduling."
        )
    if labels == [_SYSTEMD_PROBE_UNAVAILABLE]:
        return (
            "Could not query systemd for unmanaged Alfred jobs. Retry setup or inspect "
            "systemctl --user before switching this host to OSS scheduling."
        )
    if labels == [_SYSTEMD_TIMER_LOOKUP_UNAVAILABLE]:
        return (
            "Could not inspect one or more unrelated systemd timers, but no "
            "Alfred-looking scheduler jobs were found."
        )
    unreadable = [
        label.removesuffix(_LAUNCHD_UNREADABLE_SUFFIX)
        for label in labels
        if label.endswith(_LAUNCHD_UNREADABLE_SUFFIX)
    ]
    scheduler_noun = "systemd timer" if scheduler_kind == "systemd" else "launchd label"
    scheduler_command = "systemctl --user" if scheduler_kind == "systemd" else "launchctl"
    if unreadable and len(unreadable) == len(labels):
        shown = ", ".join(unreadable[:5])
        suffix = f", and {len(unreadable) - 5} more" if len(unreadable) > 5 else ""
        return (
            f"Could not verify {len(unreadable)} loaded {scheduler_noun}"
            f"{'' if len(unreadable) == 1 else 's'}: {shown}{suffix}. "
            f"Retry setup or inspect {scheduler_command} before switching this host "
            "to OSS scheduling."
        )
    shown = ", ".join(labels[:5])
    suffix = f", and {len(labels) - 5} more" if len(labels) > 5 else ""
    return (
        f"{len(labels)} unmanaged Alfred {scheduler_noun}"
        f"{'' if len(labels) == 1 else 's'} "
        f"found: {shown}{suffix}. Remove them before switching this host to OSS scheduling."
    )


def _has_config_value(env: dict[str, str], key: str) -> bool:
    return bool(_code_memory_config(env, key))


def _has_config_key(env: dict[str, str], key: str) -> bool:
    return key in env


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

    for raw in re.split(r"[\s,]+", _setup_config_value("GH_ORG") or ""):
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
        state_root = _alfred_home(dict(os.environ)) / "state"
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
