---
title: agent_runner API reference
description: Public primitives in lib/agent_runner.py. Function signatures, semantics, return shapes.
---

The shared runtime. Every codename agent imports from this module. Source: [`lib/agent_runner.py`](https://github.com/luminik-io/alfred-os/blob/main/lib/agent_runner.py).

Categorised by what the operator-facing primitive does. For deep semantics, read the source's docstrings. They're the authoritative reference.

## Path resolution + module constants

```python
HOME: Path                   # operator's home directory
ALFRED_HOME: Path            # runtime root, default ~/.alfred
WORKSPACE_ROOT: Path         # parent of per-repo checkouts, default ~/code
WORKSPACE: Path              # WORKSPACE_ROOT / "product" (back-compat alias)
GH_ORG: str                  # GitHub org slug; required for gh helpers

STATE_ROOT: Path             # ALFRED_HOME / "state"
WORKTREE_ROOT: Path          # ALFRED_HOME / "worktrees"
LIB_DIR: Path                # ALFRED_HOME / "lib"
BIN_DIR: Path                # ALFRED_HOME / "bin"

CLAUDE_BIN: str              # path to the claude CLI; default "claude"
CODEX_BIN: str               # path to the codex CLI; default "codex"
CODEX_TRANSCRIPTS_ROOT: Path # ALFRED_HOME / "state" / "codex"

GH_REPO_TO_LOCAL: dict[str, str]   # consumer-extended slug → local-dir map
STANDARD_LABELS: list[tuple]       # consumer-extended label set for ensure_labels
LIFECYCLE_LABELS: list[tuple]      # framework-provided state-machine labels

SLACK_SEVERITY_INFO: str            # "info"
SLACK_SEVERITY_WARN: str            # "warn"
SLACK_SEVERITY_ALERT: str           # "alert"
```

## Preflight + doctor mode

```python
@dataclass
class PreflightSpec:
    agent: str
    bins: list[str] = []                 # CLIs that must be on PATH
    require_gh_auth: bool = False
    aws_profile: str = ""                # if set, sts get-caller-identity must succeed under this profile
    require_workspace_repos: list[str] = []  # local checkout dirs that must exist
    env_vars: list[str] = []             # required env vars

def preflight(spec: PreflightSpec) -> None
def doctor_mode() -> bool                # reads ALFRED_DOCTOR env
```

`preflight` raises `PreflightFailed` (a `RuntimeError`) on any gap. The runner's main pattern:

```python
try:
    preflight(PREFLIGHT)
except PreflightFailed:
    return 0
if doctor_mode():
    print(f"[{AGENT.upper()}-DOCTOR-OK]")
    return 0
```

## Lock + spend + global block

```python
def with_lock(name: str)                  # mkdir-atomic per-agent mutex
class AgentLock                           # the underlying class

class SpendState:
    def __init__(self, agent: str)
    state: dict                            # firings_today, turns_today, cost_usd_today, ...
    def increment(self, **kwargs) -> None
    def set(self, **kwargs) -> None
    def is_blocked(self) -> str | None     # returns reason if rate-blocked, else None

def is_globally_blocked() -> str | None
def set_global_block(hours: int, reason: str) -> str   # returns until-iso
```

## Subprocess + shell

```python
def run(cmd: list[str], *,
        cwd: str | None = None,
        timeout: int = 60) -> subprocess.CompletedProcess

def gh_json(cmd: list[str], default: Any = None) -> Any   # gh + json parse
```

## Slack

```python
def slack_post(text: str, *,
               severity: str = "info") -> bool

# Severities: "info" (default, posted as-is), "warn" (⚠️ prefix),
# "alert" (🚨 prefix + appends <!here>).
```

Webhook URL resolution: `SLACK_WEBHOOK_URL` env -> 30-day disk cache at `$ALFRED_HOME/state/slack-webhook.cache` -> AWS Secrets Manager (`SLACK_WEBHOOK_SECRET_ID`, default `alfred/slack-webhook`).

## GitHub helpers

```python
def ensure_labels(repo_slug: str,
                  labels: list[tuple[str, str, str]] | None = None) -> None
def gh_issue_edit(repo_slug: str, num: int, *,
                  add_labels: list[str] = None,
                  remove_labels: list[str] = None) -> bool
def gh_issue_comment(repo_slug: str, num: int, body: str) -> bool
def gh_pr_create(repo_slug: str, *, title: str, body_file: Path,
                 head: str | None = None,
                 labels: list[str] | None = None,
                 base: str = "main") -> str | None      # returns PR URL
def gh_pr_comment(repo_slug: str, num: int, body: str) -> bool
```

## Issue claim state machine

See [State machine](/concepts/state-machine/) for design.

```python
def claim_issue(repo_slug: str, num: int, *,
                codename: str, firing_id: str) -> bool

def release_issue(repo_slug: str, num: int, *,
                  codename: str, firing_id: str,
                  outcome: str = "success",
                  transition_to: str | None = None,
                  pr_url: str | None = None) -> bool

def find_stale_claims(repo_slug: str, *,
                      max_age_hours: int = 4) -> list[dict]

def force_release_stale_claim(repo_slug: str, num: int, *,
                              sweep_id: str,
                              released_codename: str | None = None,
                              released_firing_id: str | None = None) -> bool

def issue_dedup_check(repo_slug: str, num: int) -> dict

# Operator overrides
def is_repo_paused(repo_slug: str) -> bool
def list_paused_repos() -> list[str]
def set_repo_paused(repo_slug: str, paused: bool) -> list[str]

# Constants
PAUSED_REPOS_FILE: Path           # state-file location
CLAIM_COMMENT_PREFIX: str         # HTML comment marker for claims
RELEASE_COMMENT_PREFIX: str       # HTML comment marker for releases
```

## Worktree management

```python
def make_worktree(local_repo: str, agent: str, target: str,
                  base: str = "origin/main") -> tuple[Path, str]   # (path, branch)
def make_worktree_from_branch(local_repo: str, agent: str,
                              head_ref: str, target: str) -> Path
def remove_worktree(local_repo: str, wt: Path) -> None
```

## Claude invocation

```python
@dataclass
class ClaudeResult:
    success: bool
    subtype: str            # "success" | "error_max_turns" | "error_budget" | "error_rate_limit" | ...
    num_turns: int
    cost_usd: float
    session_id: str | None
    result_text: str
    raw: dict
    stop_reason: str | None        # opt-in field; falls back to subtype
    error_message: str | None

def claude_invoke(prompt: str, *,
                  workdir: Path,
                  allowed_tools: str,
                  max_turns: int | None = None,
                  timeout: int = 1200) -> ClaudeResult

def claude_invoke_streaming(prompt: str, *,
                            workdir: Path,
                            allowed_tools: str,
                            agent: str,
                            firing_id: str,
                            max_turns: int | None = None,
                            timeout: int = 1200) -> ClaudeResult

def codex_invoke(prompt: str, *,
                 workdir: Path,
                 agent: str = "codex",
                 firing_id: str | None = None,
                 timeout: int = 1200,
                 model: str | None = None,
                 sandbox: str | None = None,
                 approval_policy: str | None = None,
                 add_dirs: list[Path] | None = None) -> ClaudeResult
```

The OSS streaming variant currently delegates to `claude_invoke()` while preserving the future call shape. `codex_invoke()` shells out to `codex exec`, rejects unsupported Claude-only controls (`allowed_tools`, `max_turns`, `resume_session`), defaults to `read-only` + `approval_policy=never`, and writes final-message/stdout/stderr artifacts to `$ALFRED_HOME/state/codex/<agent>/<YYYY-MM>/`.

## Event log + commit trailer + handoff table

```python
class EventLog:
    def __init__(self, agent: str, firing_id: str | None = None,
                 path: Path | None = None)
    firing_id: str
    path: Path
    def emit(self, event_type: str, **payload) -> None

def commit_trailer(agent: str, firing_id: str, *,
                   extra: dict[str, str] | None = None) -> str
class HandoffTable                # producer/consumer table for cross-codename validation
```

## Prompt loading

```python
def load_prompt(path: Path | str, *,
                extra_vars: dict[str, str] | None = None) -> str
```

Substitutes `${ENV_VAR}` from the environment (and any `extra_vars`). Unset vars stay as literals. Fails loud if you accidentally interpolate a missing var into a `gh` command.

## Conventions

- Every primitive that does network I/O has an explicit timeout and returns a status (bool / dict / dataclass) rather than raising on operational errors. Programming bugs do raise.
- Every primitive that writes operator-visible state (Slack, gh, files) is idempotent or near-idempotent.
- Every primitive that depends on the host shell uses `subprocess.run` (via `run()`), never `shell=True`.

For implementation details, the [source file](https://github.com/luminik-io/alfred-os/blob/main/lib/agent_runner.py) is exhaustively commented. Module-level docstring at the top documents the env-var contract every consumer agent inherits.
