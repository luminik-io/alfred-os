#!/usr/bin/env python3
"""Guided Batman first setup.

Batman needs a few settings that the base ``alfred-init`` flow cannot
derive safely: a Claude OAuth token for scheduler-spawned runs, a Slack
bot token for approval gates, the operator's Slack member id, and the
parent repo where large-feature issues live.

The wizard is intentionally stdlib-only and idempotent. It writes one
managed block to ``$ALFRED_HOME/.env`` and replaces that block on every run.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
SLACK_BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"
OPERATOR_USER_ENV = "ALFRED_OPERATOR_SLACK_USER_ID"
BATMAN_CHANNEL_ENV = "BATMAN_SLACK_CHANNEL"
BATMAN_PARENT_REPO_ENV = "BATMAN_PARENT_REPO"
BATMAN_AUTO_EXECUTE_ENV = "BATMAN_AUTO_EXECUTE"
BATMAN_APPROVAL_MODE_ENV = "BATMAN_APPROVAL_MODE"
BATMAN_PICKER_ENV = "BATMAN_PICKER"
BATMAN_BUNDLE_PREFIX_ENV = "BATMAN_BUNDLE_SLUG_PREFIX"
BATMAN_TIMEOUT_ENV = "BATMAN_APPROVAL_TIMEOUT_S"

MODE_HALT = "0"
MODE_APPROVAL_GATE = "approval-gate"
MODE_AUTO = "1"
VALID_MODES = (MODE_HALT, MODE_APPROVAL_GATE, MODE_AUTO)
APPROVAL_MODE_SLACK_OR_FILE = "slack-or-file"
APPROVAL_MODE_SLACK = "slack"
APPROVAL_MODE_FILE = "file"
VALID_APPROVAL_MODES = (
    APPROVAL_MODE_SLACK_OR_FILE,
    APPROVAL_MODE_SLACK,
    APPROVAL_MODE_FILE,
)
VALID_PICKERS = ("oldest", "newest")

BANNER = "# alfred-batman-setup, generated below this line. Safe to re-run."
BANNER_RE = re.compile(
    r"\r?\n?# alfred-batman-setup, generated below this line\. Safe to re-run\."
    r"(?:\r?\n(?:export )?[A-Z0-9_]+=[^\r\n]*)*\r?\n?",
    re.MULTILINE,
)

SLACK_BOT_TOKEN_RE = re.compile(r"^xoxb-[A-Za-z0-9-]{10,}$")
SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class Style:
    def __init__(self, enabled: bool) -> None:
        self.BLUE = "\033[1;34m" if enabled else ""
        self.GREEN = "\033[1;32m" if enabled else ""
        self.YELLOW = "\033[1;33m" if enabled else ""
        self.RED = "\033[1;31m" if enabled else ""
        self.OFF = "\033[0m" if enabled else ""


STYLE = Style(sys.stdout.isatty())


def step(msg: str) -> None:
    print(f"{STYLE.BLUE}==>{STYLE.OFF} {msg}")


def ok(msg: str) -> None:
    print(f"{STYLE.GREEN}  ok{STYLE.OFF} {msg}")


def warn(msg: str) -> None:
    print(f"{STYLE.YELLOW}  ! {STYLE.OFF} {msg}", file=sys.stderr)


def fail(msg: str) -> None:
    print(f"{STYLE.RED}  !!{STYLE.OFF} {msg}", file=sys.stderr)


def ask(prompt: str, default: str = "", *, non_interactive: bool = False) -> str:
    if non_interactive:
        return default
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{STYLE.BLUE}?{STYLE.OFF}  {prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def ask_yes_no(prompt: str, default: bool = False, *, non_interactive: bool = False) -> bool:
    if non_interactive:
        return default
    default_label = "Y/n" if default else "y/N"
    raw = ask(f"{prompt} [{default_label}]", "", non_interactive=False).lower()
    if not raw:
        return default
    return raw.startswith("y")


def run_cmd(cmd: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if (value.startswith("'") and value.endswith("'")) or (
            value.startswith('"') and value.endswith('"')
        ):
            value = value[1:-1]
        out[key.strip()] = value
    return out


def upsert_batman_block(path: Path, kvs: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    cleaned = BANNER_RE.sub("\n", existing).rstrip()
    lines = [BANNER]
    for key in sorted(kvs):
        value = kvs[key]
        if value == "":
            continue
        lines.append(f"{key}={shlex.quote(value)}")
    block = "\n".join(lines)
    text = f"{cleaned}\n\n{block}\n" if cleaned else f"{block}\n"

    prior_umask = os.umask(0o077)
    try:
        path.write_text(text, encoding="utf-8")
    finally:
        os.umask(prior_umask)
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def _mask(value: str, *, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def _slack_api(method: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": "non-json-response"}
    return parsed if isinstance(parsed, dict) else {"ok": False, "error": "unexpected-response"}


def validate_slack_bot_token(token: str) -> str | None:
    if not token:
        return None
    if not SLACK_BOT_TOKEN_RE.match(token):
        return "expected a Slack bot token beginning with xoxb-"
    return None


def validate_slack_user_id(user_id: str) -> str | None:
    if not user_id:
        return None
    if not SLACK_USER_ID_RE.match(user_id):
        return "expected a Slack member id beginning with U or W"
    return None


def validate_parent_repo(repo: str) -> str | None:
    if not repo:
        return None
    if not REPO_RE.match(repo):
        return "expected owner/repo"
    return None


def normalize_channel(channel: str) -> str:
    return channel.strip().lstrip("#")


def env_or_config(env: dict[str, str], config: dict[str, str], key: str, default: str = "") -> str:
    return (env.get(key) or config.get(key) or default).strip()


def infer_parent_repo(env: dict[str, str], config: dict[str, str]) -> str:
    return env_or_config(env, config, BATMAN_PARENT_REPO_ENV)


@dataclass
class BatmanSetupState:
    repo_root: Path
    alfred_home: Path
    env_file: Path
    env: dict[str, str]
    config: dict[str, str] = field(default_factory=dict)
    updates: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> BatmanSetupState:
        alfred_home = Path(
            args.alfred_home or os.environ.get("ALFRED_HOME") or Path.home() / ".alfred"
        )
        env_file = alfred_home / ".env"
        repo_root = Path(args.repo_root or Path(__file__).resolve().parent.parent)
        state = cls(
            repo_root=repo_root,
            alfred_home=alfred_home,
            env_file=env_file,
            env=dict(os.environ),
        )
        state.config = read_env_file(env_file)
        return state

    def value(self, key: str, default: str = "") -> str:
        return env_or_config({**self.env, **self.updates}, self.config, key, default)

    def has_token(self) -> bool:
        return bool(self.value(TOKEN_ENV))


def required_missing(values: dict[str, str]) -> list[str]:
    missing = []
    if not values.get(TOKEN_ENV):
        missing.append(TOKEN_ENV)
    if not values.get(BATMAN_PARENT_REPO_ENV):
        missing.append(BATMAN_PARENT_REPO_ENV)
    if approval_requires_slack(values):
        if not values.get(SLACK_BOT_TOKEN_ENV):
            missing.append(SLACK_BOT_TOKEN_ENV)
        if not values.get(OPERATOR_USER_ENV):
            missing.append(OPERATOR_USER_ENV)
    return missing


def approval_requires_slack(values: dict[str, str]) -> bool:
    return (
        values.get(BATMAN_AUTO_EXECUTE_ENV) == MODE_APPROVAL_GATE
        and values.get(BATMAN_APPROVAL_MODE_ENV, APPROVAL_MODE_SLACK_OR_FILE) != APPROVAL_MODE_FILE
    )


def render_check_only(state: BatmanSetupState) -> int:
    values = collect_values(state)
    print("Batman setup status")
    for key in (
        TOKEN_ENV,
        SLACK_BOT_TOKEN_ENV,
        OPERATOR_USER_ENV,
        BATMAN_CHANNEL_ENV,
        BATMAN_PARENT_REPO_ENV,
        BATMAN_AUTO_EXECUTE_ENV,
        BATMAN_APPROVAL_MODE_ENV,
        BATMAN_PICKER_ENV,
        BATMAN_TIMEOUT_ENV,
    ):
        value = values.get(key, "")
        if value:
            shown = _mask(value) if key in (TOKEN_ENV, SLACK_BOT_TOKEN_ENV) else value
            print(f"  ok      {key}={shown}")
        else:
            print(f"  missing {key}")
    missing = required_missing(values)
    if missing:
        print("\nMissing required setting(s): " + ", ".join(missing), file=sys.stderr)
        return 1
    return 0


def collect_values(state: BatmanSetupState) -> dict[str, str]:
    mode = state.value(BATMAN_AUTO_EXECUTE_ENV, MODE_HALT).lower() or MODE_HALT
    approval_mode = (
        state.value(BATMAN_APPROVAL_MODE_ENV, APPROVAL_MODE_SLACK_OR_FILE).lower()
        or APPROVAL_MODE_SLACK_OR_FILE
    )
    picker = state.value(BATMAN_PICKER_ENV, "oldest").lower() or "oldest"
    return {
        TOKEN_ENV: state.value(TOKEN_ENV),
        SLACK_BOT_TOKEN_ENV: state.value(SLACK_BOT_TOKEN_ENV),
        OPERATOR_USER_ENV: state.value(OPERATOR_USER_ENV),
        BATMAN_CHANNEL_ENV: normalize_channel(state.value(BATMAN_CHANNEL_ENV)),
        BATMAN_PARENT_REPO_ENV: state.value(BATMAN_PARENT_REPO_ENV)
        or infer_parent_repo(state.env, state.config),
        BATMAN_AUTO_EXECUTE_ENV: mode if mode in VALID_MODES else MODE_HALT,
        BATMAN_APPROVAL_MODE_ENV: (
            approval_mode if approval_mode in VALID_APPROVAL_MODES else APPROVAL_MODE_SLACK_OR_FILE
        ),
        BATMAN_PICKER_ENV: picker if picker in VALID_PICKERS else "oldest",
        BATMAN_BUNDLE_PREFIX_ENV: state.value(BATMAN_BUNDLE_PREFIX_ENV),
        BATMAN_TIMEOUT_ENV: state.value(BATMAN_TIMEOUT_ENV, "900") or "900",
    }


def step_preflight(state: BatmanSetupState) -> None:
    step("Preflight")
    state.alfred_home.mkdir(parents=True, exist_ok=True)
    state.env_file.parent.mkdir(parents=True, exist_ok=True)
    if state.env_file.exists():
        ok(f"{state.env_file} found")
    else:
        state.env_file.write_text("", encoding="utf-8")
        with contextlib.suppress(OSError):
            state.env_file.chmod(0o600)
        ok(f"{state.env_file} created")
    gh_org = state.value("GH_ORG")
    if gh_org:
        ok(f"GH_ORG={gh_org}")
    else:
        warn("GH_ORG is not set. Parent repo must be entered as owner/repo.")


def step_claude_oauth(
    state: BatmanSetupState,
    *,
    non_interactive: bool,
    skip_token_setup: bool,
) -> None:
    step("Claude OAuth token")
    if state.has_token():
        ok(f"{TOKEN_ENV} already configured")
        return
    warn(f"{TOKEN_ENV} is not set")
    if skip_token_setup:
        warn("Skipping token setup. Scheduled Batman firings will fail until the token is set.")
        return
    if non_interactive:
        warn(
            "Non-interactive mode cannot run `alfred setup-token`; pass --skip-token-setup or set the token first."
        )
        return
    if ask_yes_no("Run `alfred setup-token` now?", True):
        script = Path(__file__).resolve().parent / "alfred-setup-token.py"
        rc = subprocess.run([sys.executable, str(script)], check=False).returncode
        if rc == 0:
            state.config = read_env_file(state.env_file)
            ok(f"{TOKEN_ENV} configured")
        else:
            warn(f"`alfred setup-token` exited {rc}. Re-run it before enabling Batman.")
    else:
        warn("Skipped token setup.")


def step_slack_scopes() -> None:
    step("Slack bot scopes")
    print("Required scopes for Batman approval gates:")
    print("  - chat:write")
    print("  - reactions:read")
    print("  - channels:history for public channels, or groups:history for private channels")
    print("Reinstall the Slack app after changing scopes, then copy the bot token.")


def step_slack_token(
    state: BatmanSetupState,
    args: argparse.Namespace,
    *,
    values: dict[str, str],
    non_interactive: bool,
) -> None:
    step("Slack bot token")
    token = args.slack_bot_token or values[SLACK_BOT_TOKEN_ENV]
    if args.force or not token:
        token = ask("Bot token (xoxb-...)", token, non_interactive=non_interactive)
    problem = validate_slack_bot_token(token)
    if problem:
        fail(f"{SLACK_BOT_TOKEN_ENV}: {problem}")
        raise SystemExit(1)
    if token:
        state.updates[SLACK_BOT_TOKEN_ENV] = token
        ok(f"{SLACK_BOT_TOKEN_ENV}={_mask(token)}")
        if args.run_slack_tests:
            resp = _slack_api("auth.test", token, {})
            if resp.get("ok"):
                ok(f"Slack auth.test ok (bot user: {resp.get('user', 'unknown')})")
            else:
                fail(f"Slack auth.test failed: {resp.get('error', 'unknown')}")
                raise SystemExit(1)
    elif approval_requires_slack(values):
        fail(f"{SLACK_BOT_TOKEN_ENV} is required for approval-gate mode")
        raise SystemExit(1)
    else:
        warn("No Slack bot token set. Approval-gate mode will remain unavailable.")


def step_operator_user(
    state: BatmanSetupState,
    args: argparse.Namespace,
    *,
    values: dict[str, str],
    non_interactive: bool,
) -> None:
    step("Operator Slack member ID")
    user_id = args.operator_user_id or values[OPERATOR_USER_ENV]
    if args.force or not user_id:
        user_id = ask("Member ID (U...)", user_id, non_interactive=non_interactive)
    problem = validate_slack_user_id(user_id)
    if problem:
        fail(f"{OPERATOR_USER_ENV}: {problem}")
        raise SystemExit(1)
    if user_id:
        state.updates[OPERATOR_USER_ENV] = user_id
        ok(f"{OPERATOR_USER_ENV}={user_id}")
        token = state.updates.get(SLACK_BOT_TOKEN_ENV) or values[SLACK_BOT_TOKEN_ENV]
        if args.run_slack_tests and token:
            resp = _slack_api("users.info", token, {"user": user_id})
            if resp.get("ok"):
                name = (resp.get("user") or {}).get("profile", {}).get("real_name") or user_id
                ok(f"Slack users.info ok (user: {name})")
            else:
                fail(f"Slack users.info failed: {resp.get('error', 'unknown')}")
                raise SystemExit(1)
    elif approval_requires_slack(values):
        fail(f"{OPERATOR_USER_ENV} is required for approval-gate mode")
        raise SystemExit(1)
    else:
        warn("No operator Slack member ID set. Approval-gate mode will remain unavailable.")


def step_channel(
    state: BatmanSetupState,
    args: argparse.Namespace,
    *,
    values: dict[str, str],
    non_interactive: bool,
) -> None:
    step("Approval channel")
    channel = normalize_channel(args.slack_channel or values[BATMAN_CHANNEL_ENV])
    if args.force or not channel:
        channel = normalize_channel(
            ask(
                "Channel name or id (blank uses SLACK_HOME_CHANNEL fallback)",
                channel,
                non_interactive=non_interactive,
            )
        )
    if channel:
        state.updates[BATMAN_CHANNEL_ENV] = channel
        ok(f"{BATMAN_CHANNEL_ENV}={channel}")
    else:
        warn("No Batman channel set. Runtime will fall back to SLACK_HOME_CHANNEL or alfred.")


def step_parent_repo(
    state: BatmanSetupState,
    args: argparse.Namespace,
    *,
    values: dict[str, str],
    non_interactive: bool,
) -> None:
    step("Parent repo")
    parent_repo = args.parent_repo or values[BATMAN_PARENT_REPO_ENV]
    if args.force or not parent_repo:
        parent_repo = ask(
            "Parent repo for agent:large-feature issues",
            parent_repo,
            non_interactive=non_interactive,
        )
    problem = validate_parent_repo(parent_repo)
    if problem:
        fail(f"{BATMAN_PARENT_REPO_ENV}: {problem}")
        raise SystemExit(1)
    if not parent_repo:
        fail(f"{BATMAN_PARENT_REPO_ENV} is required")
        raise SystemExit(1)
    state.updates[BATMAN_PARENT_REPO_ENV] = parent_repo
    ok(f"{BATMAN_PARENT_REPO_ENV}={parent_repo}")


def step_mode(
    state: BatmanSetupState,
    args: argparse.Namespace,
    *,
    values: dict[str, str],
    non_interactive: bool,
) -> None:
    step("Approval gate behaviour")
    print("  a) approval-gate: post a plan, wait for approval, then file children")
    print("  b) 1: execute immediately after planning")
    print("  c) 0: halt after plan")
    configured_mode = state.value(BATMAN_AUTO_EXECUTE_ENV)
    default_mode = args.mode or configured_mode or MODE_APPROVAL_GATE
    if default_mode == "a":
        default_mode = MODE_APPROVAL_GATE
    if default_mode == "b":
        default_mode = MODE_AUTO
    if default_mode == "c":
        default_mode = MODE_HALT
    raw = default_mode
    if args.force or not args.mode:
        raw = ask("Choice", default_mode, non_interactive=non_interactive).lower()
    mode_map = {"a": MODE_APPROVAL_GATE, "b": MODE_AUTO, "c": MODE_HALT}
    mode = mode_map.get(raw, raw)
    if mode not in VALID_MODES:
        fail(f"{BATMAN_AUTO_EXECUTE_ENV}: expected one of {', '.join(VALID_MODES)}")
        raise SystemExit(1)
    state.updates[BATMAN_AUTO_EXECUTE_ENV] = mode
    ok(f"{BATMAN_AUTO_EXECUTE_ENV}={mode}")


def step_approval_mode(
    state: BatmanSetupState,
    args: argparse.Namespace,
    *,
    values: dict[str, str],
    non_interactive: bool,
) -> None:
    if state.value(BATMAN_AUTO_EXECUTE_ENV) != MODE_APPROVAL_GATE:
        return
    step("Approval surface")
    print("  a) slack-or-file: Slack reactions plus Alfred client approve/decline")
    print("  b) slack: Slack reactions only")
    print("  c) file: Alfred client/file marker only")
    default_mode = (
        args.approval_mode or values[BATMAN_APPROVAL_MODE_ENV] or APPROVAL_MODE_SLACK_OR_FILE
    )
    mode_map = {
        "a": APPROVAL_MODE_SLACK_OR_FILE,
        "b": APPROVAL_MODE_SLACK,
        "c": APPROVAL_MODE_FILE,
    }
    raw = default_mode
    if args.force or not args.approval_mode:
        raw = ask("Choice", default_mode, non_interactive=non_interactive).lower()
    mode = mode_map.get(raw, raw)
    if mode not in VALID_APPROVAL_MODES:
        fail(f"{BATMAN_APPROVAL_MODE_ENV}: expected one of {', '.join(VALID_APPROVAL_MODES)}")
        raise SystemExit(1)
    state.updates[BATMAN_APPROVAL_MODE_ENV] = mode
    ok(f"{BATMAN_APPROVAL_MODE_ENV}={mode}")


def step_optional_knobs(
    state: BatmanSetupState,
    args: argparse.Namespace,
    *,
    values: dict[str, str],
) -> None:
    picker = (args.picker or values[BATMAN_PICKER_ENV] or "oldest").lower()
    if picker not in VALID_PICKERS:
        fail(f"{BATMAN_PICKER_ENV}: expected oldest or newest")
        raise SystemExit(1)
    timeout = str(
        args.approval_timeout_s
        if args.approval_timeout_s is not None
        else values[BATMAN_TIMEOUT_ENV] or "900"
    )
    if not timeout.isdigit() or int(timeout) < 0:
        fail(f"{BATMAN_TIMEOUT_ENV}: expected a non-negative integer")
        raise SystemExit(1)
    state.updates[BATMAN_PICKER_ENV] = picker
    state.updates[BATMAN_TIMEOUT_ENV] = timeout
    prefix = (
        args.bundle_slug_prefix
        if args.bundle_slug_prefix is not None
        else values[BATMAN_BUNDLE_PREFIX_ENV]
    )
    if prefix:
        state.updates[BATMAN_BUNDLE_PREFIX_ENV] = prefix


def step_doctor(state: BatmanSetupState, *, skip_doctor: bool) -> None:
    step("Smoke test")
    if skip_doctor:
        warn("Skipping lifecycle doctor.")
        return
    doctor = state.repo_root / "bin" / "doctor.sh"
    if not doctor.exists():
        warn(f"{doctor} not found; skipping lifecycle doctor.")
        return
    result = run_cmd(["bash", str(doctor), "--lifecycle"], timeout=120)
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.stderr.strip():
        print(result.stderr.rstrip(), file=sys.stderr)
    if result.returncode != 0:
        fail("Lifecycle doctor failed. Fix the reported config and re-run this wizard.")
        raise SystemExit(result.returncode)
    ok("lifecycle doctor passed")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Guided Batman setup wizard.")
    parser.add_argument(
        "--check-only", action="store_true", help="report setup state without writing"
    )
    parser.add_argument(
        "--force", action="store_true", help="prompt for values even when configured"
    )
    parser.add_argument(
        "--non-interactive", action="store_true", help="accept defaults and provided flags"
    )
    parser.add_argument(
        "--skip-token-setup", action="store_true", help="do not spawn alfred setup-token"
    )
    parser.add_argument(
        "--skip-doctor", action="store_true", help="do not run bin/doctor.sh --lifecycle"
    )
    parser.add_argument(
        "--run-slack-tests", action="store_true", help="call Slack auth.test and users.info"
    )
    parser.add_argument("--slack-bot-token", default="", help="Slack bot token to persist")
    parser.add_argument(
        "--operator-user-id", default="", help="Slack member id whose reactions count"
    )
    parser.add_argument(
        "--slack-channel", default="", help="Slack channel name or id for Batman posts"
    )
    parser.add_argument(
        "--parent-repo", default="", help="owner/repo that holds Batman parent issues"
    )
    parser.add_argument("--mode", choices=VALID_MODES, default="", help="Batman execution mode")
    parser.add_argument(
        "--approval-mode",
        choices=VALID_APPROVAL_MODES,
        default="",
        help="approval surface for approval-gate mode",
    )
    parser.add_argument("--picker", choices=VALID_PICKERS, default="", help="parent issue picker")
    parser.add_argument("--bundle-slug-prefix", default=None, help="optional bundle slug prefix")
    parser.add_argument(
        "--approval-timeout-s", type=int, default=None, help="approval wait timeout seconds"
    )
    parser.add_argument("--alfred-home", default="", help=argparse.SUPPRESS)
    parser.add_argument("--repo-root", default="", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    non_interactive = args.non_interactive or bool(os.environ.get("ALFRED_NONINTERACTIVE"))
    state = BatmanSetupState.from_args(args)

    if args.check_only:
        return render_check_only(state)

    print(f"{STYLE.BLUE}alfred-batman-setup{STYLE.OFF} guided setup.")
    print(f"  Repo:        {state.repo_root}")
    print(f"  ALFRED_HOME: {state.alfred_home}")
    print(f"  .env:        {state.env_file}")
    print()

    step_preflight(state)
    step_claude_oauth(
        state,
        non_interactive=non_interactive,
        skip_token_setup=args.skip_token_setup,
    )
    values = collect_values(state)
    step_mode(state, args, values=values, non_interactive=non_interactive)
    values = collect_values(state)
    step_approval_mode(state, args, values=values, non_interactive=non_interactive)
    values = collect_values(state)
    if approval_requires_slack(values):
        step_slack_scopes()
        step_slack_token(state, args, values=values, non_interactive=non_interactive)
        values = collect_values(state)
        step_operator_user(state, args, values=values, non_interactive=non_interactive)
        values = collect_values(state)
        step_channel(state, args, values=values, non_interactive=non_interactive)
        values = collect_values(state)
    else:
        warn("Skipping Slack approval setup for this Batman mode.")
    step_parent_repo(state, args, values=values, non_interactive=non_interactive)
    values = collect_values(state)
    step_optional_knobs(state, args, values=values)

    upsert_batman_block(state.env_file, state.updates)
    ok(f"wrote {len(state.updates)} Batman setting(s) to {state.env_file}")
    step_doctor(state, skip_doctor=args.skip_doctor)

    print("\nDone. Next steps:")
    print("  1. Read docs/BATMAN_PARENT_ISSUE_TEMPLATE.md for the parent issue format.")
    print(f"  2. File an agent:large-feature issue in {state.updates[BATMAN_PARENT_REPO_ENV]}.")
    if state.updates.get(BATMAN_AUTO_EXECUTE_ENV) != MODE_APPROVAL_GATE:
        print("  3. Review Batman's drafted plan before filing child work.")
    elif state.updates.get(BATMAN_APPROVAL_MODE_ENV) == APPROVAL_MODE_FILE:
        print("  3. Approve or decline Batman plans from the Alfred client.")
    else:
        print("  3. Watch the configured Slack channel for Batman's plan post.")
        print("  4. React with :white_check_mark: to approve, or :x: to reject.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
