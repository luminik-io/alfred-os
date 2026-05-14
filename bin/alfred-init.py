#!/usr/bin/env python3
"""alfred-init, interactive fleet configuration wizard.

Run after `git clone` + `bash install.sh`. install.sh handles dependency
install (brew, gh, claude, aws, python, node, runtime dirs, ~/.alfredrc
template). alfred-init handles configuration: auth checks, Slack webhook
provisioning, agent selection, codename + repo + schedule wiring,
agents.conf generation, deploy.sh, doctor.sh, smoke test.

Wizard order (each step is idempotent, re-running won't duplicate):
    0. Preflight:      ALFRED_HOME, ~/.alfredrc, GH_ORG must exist.
    1. Claude Code:    `claude --version` + non-interactive auth probe.
    2. GitHub:         `gh auth status` + cache `gh repo list <GH_ORG>`.
    3. Slack webhook:  guide the operator, validate, test-post, store
                         (env or AWS Secrets Manager).
    4. AWS (optional) , per-agent IAM profiles for Huntress / Gordon if
                         the operator wants them.
    5. Pick agents:    multi-select discovered from bin/*.py.
    6. Codenames:      per-role codename (default = canonical Batman name).
    7. Repos:          per-agent repo selection out of `gh repo list`.
    8. Schedule:       sensible defaults; press 'a' to customize.
    9. Generate config, write agents.conf + per-agent env to ~/.alfredrc.
   10. Deploy:         `bash deploy.sh`.
   11. Doctor:         `bash bin/doctor.sh`.
   12. Smoke test:     final Slack post + summary.

Override paths:
    ALFRED_NONINTERACTIVE=1   accept defaults everywhere
    ALFRED_DOCTOR=1               print [ALFRED-INIT-DOCTOR-OK] and exit
    --non-interactive             same as the env var
    --config <path>               read answers from JSON (skip prompts)
    --agents <comma>              skip the agent multi-select

Pure stdlib. The operator reads this file when something breaks; keep it
that way, no external deps, no clever indirection.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants, the canonical Batman-codename map.
# ---------------------------------------------------------------------------

# role-key -> (default codename, one-line description, operates_on_repos,
# default schedule string in launchd/agents.conf format)
AGENT_CATALOG: dict[str, tuple[str, str, bool, str]] = {
    "feature_dev": (
        "lucius",
        "feature dev (picks agent:implement issues, opens PRs)",
        True,
        "interval:1200",
    ),
    "planner": (
        "drake",
        "issue planner (files agent:implement issues from specs)",
        True,
        "interval:7200",
    ),
    "test_coverage": (
        "bane",
        "test coverage (writes tests for low-coverage changed files)",
        True,
        "interval:14400",
    ),
    "pr_review": ("rasalghul", "PR review (multi-axis on every fresh PR)", True, "interval:1800"),
    "ci_repair": (
        "nightwing",
        "CI repair (re-runs flaky checks, opens fix PRs)",
        True,
        "interval:2700",
    ),
    "doc_writer": ("robin", "doc writer (keeps READMEs and ADRs in sync)", True, "interval:10800"),
    "smoke_runner": (
        "huntress",
        "staging smoke runner (hits a URL on schedule)",
        False,
        "interval:1800",
    ),
    "ops_morning": ("gordon", "ops morning (ECS + Sentry health roll-up)", False, "cron:8:00"),
    "automerge": ("automerge", "PR automerge (merges green, blessed PRs)", False, "interval:900"),
    "agent_cleanup": (
        "agent-cleanup",
        "agent cleanup (prunes stale claims + worktrees)",
        False,
        "cron:3:00",
    ),
    "code_map_refresh": (
        "code-map-refresh",
        "code map refresh (regenerates per-repo skeleton)",
        True,
        "interval:21600",
    ),
    "morning_brief": (
        "agent-morning-brief",
        "morning brief (overnight fleet summary)",
        False,
        "cron:7:00",
    ),
    "fleet_doctor": (
        "fleet-doctor",
        "fleet doctor (daily local health snapshot)",
        False,
        "cron:7:30",
    ),
    "fleet_recap_morning": (
        "fleet-recap-morning",
        "fleet recap morning (7:30 status post)",
        False,
        "cron:7:45",
    ),
    "fleet_recap_evening": (
        "fleet-recap-evening",
        "fleet recap evening (22:00 status post)",
        False,
        "cron:22:00",
    ),
    "shipped_summary_daily": (
        "shipped-summary-daily",
        "shipped summary daily (merged PRs, issues, LOC)",
        False,
        "cron:7:35",
    ),
    "shipped_summary_weekly": (
        "shipped-summary-weekly",
        "shipped summary weekly (merged PRs, issues, LOC)",
        False,
        "cron:1:7:35",
    ),
}

# Map default codename -> role-key (for discovery from bin/*.py).
CODENAME_TO_ROLE: dict[str, str] = {
    default: role for role, (default, _, _, _) in AGENT_CATALOG.items()
}

# Repo-operating agents that need a staging URL / cluster name beyond repos.
SPECIAL_PROMPTS = {
    "huntress": [("ALFRED_HUNTRESS_TARGET_URL", "Staging URL Huntress should hit")],
    "gordon": [
        ("ALFRED_GORDON_ECS_CLUSTER", "ECS cluster name for Gordon"),
        ("ALFRED_GORDON_SENTRY_ORG", "Sentry org slug for Gordon (blank to skip)"),
    ],
}

CODENAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
SLACK_WEBHOOK_RE = re.compile(r"^https://hooks\.slack\.com/services/")

ALFREDRC_BANNER = "# alfred-init, generated below this line. Safe to re-run."
# Matches the banner whatever separator a past release used between
# "alfred-init" and "generated" (older releases used an em-dash, current
# uses a comma). upsert_alfredrc relies on this so an upgrade rewrites the
# existing managed block in place instead of appending a duplicate.
ALFREDRC_BANNER_RE = re.compile(r"# alfred-init.{1,4}generated below this line\. Safe to re-run\.")


# ---------------------------------------------------------------------------
# ANSI helpers (TTY-aware).
# ---------------------------------------------------------------------------


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.BLUE = "\033[1;34m" if enabled else ""
        self.GREEN = "\033[1;32m" if enabled else ""
        self.YELLOW = "\033[1;33m" if enabled else ""
        self.RED = "\033[1;31m" if enabled else ""
        self.DIM = "\033[2m" if enabled else ""
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


def note(msg: str) -> None:
    print(f"{STYLE.DIM}     {msg}{STYLE.OFF}")


# ---------------------------------------------------------------------------
# Config dataclass, single source of truth for what the wizard collects.
# ---------------------------------------------------------------------------


@dataclass
class WizardState:
    alfred_home: Path
    alfredrc: Path
    repo_root: Path
    gh_org: str = ""
    repos: list[str] = field(default_factory=list)
    slack_webhook: str = ""
    slack_storage: str = "env"  # "env" or "aws"
    aws_profile_for_slack: str = ""
    aws_region: str = "us-east-1"
    use_aws: bool = False
    aws_agent_profiles: dict[str, str] = field(default_factory=dict)  # codename -> profile
    enabled_roles: list[str] = field(default_factory=list)  # role keys
    role_to_codename: dict[str, str] = field(default_factory=dict)  # role -> codename
    role_to_repos: dict[str, list[str]] = field(default_factory=dict)  # role -> [org/repo]
    role_to_schedule: dict[str, str] = field(default_factory=dict)  # role -> schedule
    role_to_extras: dict[str, dict[str, str]] = field(default_factory=dict)  # role -> {ENV: value}

    def codename_for(self, role: str) -> str:
        return self.role_to_codename.get(role, AGENT_CATALOG[role][0])


# ---------------------------------------------------------------------------
# Prompt helpers.
# ---------------------------------------------------------------------------


def ask(prompt: str, default: str = "", *, non_interactive: bool = False) -> str:
    if non_interactive:
        return default
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{STYLE.BLUE}?{STYLE.OFF}  {prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return ans or default


def ask_yes_no(prompt: str, default: bool = False, *, non_interactive: bool = False) -> bool:
    default_str = "Y/n" if default else "y/N"
    if non_interactive:
        return default
    raw = ask(f"{prompt} [{default_str}]", "", non_interactive=False).lower()
    if not raw:
        return default
    return raw.startswith("y")


# ---------------------------------------------------------------------------
# Subprocess helpers.
# ---------------------------------------------------------------------------


def run(
    cmd: list[str],
    *,
    check: bool = False,
    capture: bool = True,
    timeout: int | None = None,
    input_str: str | None = None,
) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run with sane defaults."""
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        timeout=timeout,
        input=input_str,
    )


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


# ---------------------------------------------------------------------------
# .alfredrc IO, append-only with idempotent guard markers.
# ---------------------------------------------------------------------------


def read_alfredrc(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE pairs from ~/.alfredrc. Quotes/exports are tolerated."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        if k:
            out[k] = v
    return out


def upsert_alfredrc(path: Path, kvs: dict[str, str]) -> None:
    """Add or update keys in ~/.alfredrc below the alfred-init banner.

    Idempotent: rewrites the marker block on every call so re-running
    the wizard doesn't accumulate dupes.
    """
    if not kvs:
        return
    existing = path.read_text() if path.exists() else ""
    # Strip any prior alfred-init block (current or older-release banner) so
    # we re-emit fresh values instead of accumulating a duplicate section.
    prior = ALFREDRC_BANNER_RE.search(existing)
    if prior:
        existing = existing[: prior.start()].rstrip() + "\n"
    block = [ALFREDRC_BANNER]
    for k, v in kvs.items():
        block.append(f"{k}={v}")
    new = existing.rstrip() + "\n\n" + "\n".join(block) + "\n"
    path.write_text(new)
    with contextlib.suppress(OSError):
        path.chmod(0o600)


# ---------------------------------------------------------------------------
# Agent discovery - scan bin/*.py for known role runners.
# ---------------------------------------------------------------------------


def discover_agents(bin_dir: Path) -> list[str]:
    """Return the role-keys from AGENT_CATALOG whose runners exist in bin/.

    A runner is "present" when the role script in AGENT_CATALOG exists.
    Custom codenames change the launchd label and AGENT_CODENAME, not the
    stable role script. Order is the
    canonical AGENT_CATALOG order, operators see them in the same order
    every run.
    """
    if not bin_dir.is_dir():
        return []
    present = set()
    for f in bin_dir.iterdir():
        if not f.is_file() or f.suffix != ".py":
            continue
        stem = f.stem
        if stem in CODENAME_TO_ROLE:
            present.add(CODENAME_TO_ROLE[stem])
    # Some agents are .sh (fleet-recap-morning/evening). Check those too.
    for f in bin_dir.iterdir():
        if not f.is_file() or f.suffix != ".sh":
            continue
        stem = f.stem
        # fleet-recap.sh ships the morning + evening jobs.
        if stem == "fleet-recap":
            present.add("fleet_recap_morning")
            present.add("fleet_recap_evening")
        if stem in CODENAME_TO_ROLE:
            present.add(CODENAME_TO_ROLE[stem])
    return [role for role in AGENT_CATALOG if role in present]


# ---------------------------------------------------------------------------
# agents.conf renderer.
# ---------------------------------------------------------------------------


def render_agents_conf(state: WizardState) -> str:
    """Produce the full agents.conf text from WizardState.

    Format mirrors launchd/agents.conf.example: tab-separated rows
    (label, script, schedule, needs_java, log_stem, role). Includes a header
    comment so the operator can find their way back later.
    """
    lines = [
        "# agents.conf, generated by alfred-init.",
        "# Tab-separated. Re-run `alfred-init` to regenerate.",
        "#",
        "# label\tscript\tschedule\tneeds_java\tlog_stem\trole",
        "",
    ]
    for role in state.enabled_roles:
        codename = state.codename_for(role)
        default_codename, desc, _, _ = AGENT_CATALOG[role]
        schedule = state.role_to_schedule.get(role, AGENT_CATALOG[role][3])
        # Paired schedule rows share one implementation + log stem.
        if role.startswith("fleet_recap_"):
            script = "fleet-recap.sh"
            log_stem = "alfred.fleet-recap"
        elif role.startswith("shipped_summary_"):
            script = f"{default_codename}.sh"
            log_stem = "alfred.shipped-summary"
        else:
            # Script names are stable role implementations. Custom codenames
            # change the launchd label and AGENT_CODENAME, not the file name.
            script = f"{default_codename}.py"
            log_stem = f"alfred.{codename}"
        label = f"alfred.{codename}"
        role_text = desc.split(" (", 1)[0]
        lines.append(f"{label}\t{script}\t{schedule}\tno\t{log_stem}\t{role_text}")
    return "\n".join(lines) + "\n"


def env_assignments_for(state: WizardState) -> dict[str, str]:
    """Per-role env-var map written into ~/.alfredrc."""
    out: dict[str, str] = {}
    if state.gh_org:
        out["GH_ORG"] = state.gh_org
    if state.slack_storage == "env" and state.slack_webhook:
        out["SLACK_WEBHOOK_URL"] = state.slack_webhook
    elif state.slack_storage == "aws":
        out["SLACK_WEBHOOK_SECRET_ID"] = "alfred/slack-webhook"
        out["SLACK_WEBHOOK_SECRET_REGION"] = state.aws_region
    for role in state.enabled_roles:
        codename = state.codename_for(role)
        default_codename = AGENT_CATALOG[role][0]
        default_slug = default_codename.upper().replace("-", "_")
        out[f"AGENT_CODENAME_{role.upper()}"] = codename
        repos = state.role_to_repos.get(role, [])
        if repos:
            out[f"ALFRED_{default_slug}_REPOS"] = ",".join(repos)
        for k, v in state.role_to_extras.get(role, {}).items():
            out[k] = v
        if state.use_aws and codename in state.aws_agent_profiles:
            out[f"ALFRED_{default_slug}_AWS_PROFILE"] = state.aws_agent_profiles[codename]
    return out


# ---------------------------------------------------------------------------
# Slack webhook test post.
# ---------------------------------------------------------------------------


def slack_post(webhook: str, text: str, *, timeout: int = 10) -> tuple[bool, str]:
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if 200 <= resp.status < 300:
                return True, body
            return False, f"HTTP {resp.status}: {body}"
    except urllib.error.URLError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Wizard steps.
# ---------------------------------------------------------------------------


def step_0_preflight(state: WizardState) -> None:
    step("Preflight")
    if not state.alfred_home.is_dir():
        fail(f"ALFRED_HOME ({state.alfred_home}) not found.")
        fail("Run `bash install.sh` first.")
        sys.exit(1)
    ok(f"ALFRED_HOME: {state.alfred_home}")
    if not state.alfredrc.exists():
        fail(f"~/.alfredrc not found at {state.alfredrc}.")
        fail("Run `bash install.sh` first.")
        sys.exit(1)
    ok(f"~/.alfredrc: {state.alfredrc}")
    rc = read_alfredrc(state.alfredrc)
    state.gh_org = os.environ.get("GH_ORG") or rc.get("GH_ORG", "")
    if not state.gh_org:
        state.gh_org = ask("GH_ORG (GitHub org/user for your fleet)", "")
        if not state.gh_org:
            fail("GH_ORG required. Add it to ~/.alfredrc and re-run.")
            sys.exit(1)
    ok(f"GH_ORG: {state.gh_org}")
    note("Run with ALFRED_NONINTERACTIVE=1 for non-interactive defaults.")


def step_1_claude(*, non_interactive: bool) -> None:
    step("Claude Code auth")
    if not have("claude"):
        fail("`claude` not on PATH. Install: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)
    cp = run(["claude", "--version"], timeout=10)
    if cp.returncode != 0:
        fail(f"`claude --version` failed: {cp.stderr.strip()}")
        sys.exit(1)
    ok(f"claude: {cp.stdout.strip() or '(installed)'}")
    # Light auth probe, bounded, doesn't burn a real turn if it errors.
    try:
        probe = run(["claude", "-p", "--max-turns", "1"], input_str="say hi\n", timeout=30)
    except subprocess.TimeoutExpired:
        warn("`claude -p` probe timed out. Likely waiting on auth.")
        if not non_interactive:
            input("Run `claude` interactively to authenticate, then press Enter to continue. ")
        return
    blob = (probe.stdout + probe.stderr).lower()
    if probe.returncode != 0 and ("login" in blob or "auth" in blob or "unauthorized" in blob):
        warn("Claude auth check failed. Run `claude` interactively to log in.")
        if not non_interactive:
            input("Press Enter once you have authenticated. ")
    else:
        ok("claude responds non-interactively")


def step_2_github(state: WizardState, *, non_interactive: bool) -> None:
    step("GitHub auth")
    if not have("gh"):
        fail("`gh` not on PATH. Install via `brew install gh`.")
        sys.exit(1)
    auth = run(["gh", "auth", "status"], timeout=15)
    if auth.returncode != 0:
        warn("`gh auth status` reports not authenticated.")
        if not non_interactive:
            input("Run `gh auth login` in another shell, then press Enter. ")
            auth = run(["gh", "auth", "status"], timeout=15)
            if auth.returncode != 0:
                fail("Still not authenticated. Aborting.")
                sys.exit(1)
        else:
            sys.exit(1)
    ok("gh authenticated")
    repos_cp = run(
        ["gh", "repo", "list", state.gh_org, "--limit", "200", "--json", "nameWithOwner"],
        timeout=30,
    )
    if repos_cp.returncode != 0:
        fail(f"`gh repo list {state.gh_org}` failed: {repos_cp.stderr.strip()}")
        sys.exit(1)
    try:
        rows = json.loads(repos_cp.stdout or "[]")
    except json.JSONDecodeError:
        rows = []
    state.repos = sorted({r.get("nameWithOwner", "") for r in rows if r.get("nameWithOwner")})
    if not state.repos:
        warn(
            f"No repos visible in {state.gh_org}. You can still proceed, but per-agent repo prompts will be blank."
        )
    else:
        ok(f"{len(state.repos)} repos visible in {state.gh_org}")


def step_3_slack(state: WizardState, *, non_interactive: bool) -> None:
    step("Slack webhook")
    note("1. Open https://api.slack.com/apps in your browser.")
    note("2. Create a new app from scratch.")
    note("3. Add Incoming Webhooks; activate them.")
    note("4. Add a webhook URL to the channel you want for fleet status.")
    note("5. Copy the resulting URL.")
    while True:
        url = ask(
            "Paste your Slack webhook URL (or 'skip')", "skip", non_interactive=non_interactive
        )
        if url == "skip" or not url:
            warn("Skipping Slack setup. Agents that depend on slack_post will degrade quietly.")
            return
        if not SLACK_WEBHOOK_RE.match(url):
            fail("That doesn't look like a Slack webhook URL. Try again.")
            continue
        success, body = slack_post(url, "alfred-os installer: webhook test ok")
        if success:
            ok("Webhook test post succeeded.")
            state.slack_webhook = url
            break
        fail(f"Test post failed: {body}")
        if not ask_yes_no("Retry?", True, non_interactive=non_interactive):
            return
    storage = ask(
        "Store webhook in AWS Secrets Manager (recommended for prod) or env? [aws/env]",
        "env",
        non_interactive=non_interactive,
    ).lower()
    if storage == "aws":
        if not have("aws"):
            warn("`aws` CLI not found; falling back to env-var storage.")
            state.slack_storage = "env"
            return
        profile = ask(
            "AWS profile name (admin) for writing the secret",
            "default",
            non_interactive=non_interactive,
        )
        region = ask("AWS region", state.aws_region, non_interactive=non_interactive)
        ident = run(["aws", "--profile", profile, "sts", "get-caller-identity"], timeout=20)
        if ident.returncode != 0:
            fail(f"AWS identity check failed: {ident.stderr.strip()}")
            warn("Falling back to env-var storage.")
            state.slack_storage = "env"
            return
        create = run(
            [
                "aws",
                "--profile",
                profile,
                "--region",
                region,
                "secretsmanager",
                "create-secret",
                "--name",
                "alfred/slack-webhook",
                "--secret-string",
                state.slack_webhook,
            ],
            timeout=30,
        )
        if create.returncode != 0:
            if "ResourceExistsException" in create.stderr or "already exists" in create.stderr:
                if ask_yes_no("Secret exists. Update it?", True, non_interactive=non_interactive):
                    upd = run(
                        [
                            "aws",
                            "--profile",
                            profile,
                            "--region",
                            region,
                            "secretsmanager",
                            "update-secret",
                            "--secret-id",
                            "alfred/slack-webhook",
                            "--secret-string",
                            state.slack_webhook,
                        ],
                        timeout=30,
                    )
                    if upd.returncode != 0:
                        fail(f"Update failed: {upd.stderr.strip()}")
                        warn("Falling back to env-var storage.")
                        state.slack_storage = "env"
                        return
            else:
                fail(f"Secret create failed: {create.stderr.strip()}")
                warn("Falling back to env-var storage.")
                state.slack_storage = "env"
                return
        ok("Slack webhook stored in AWS Secrets Manager (alfred/slack-webhook)")
        state.slack_storage = "aws"
        state.aws_profile_for_slack = profile
        state.aws_region = region
    else:
        state.slack_storage = "env"
        ok("Slack webhook will be written to ~/.alfredrc as SLACK_WEBHOOK_URL")


def step_4_aws(state: WizardState, *, non_interactive: bool) -> None:
    step("AWS (optional, per-agent IAM)")
    if not ask_yes_no(
        "Use AWS for per-agent IAM and Secrets Manager?", False, non_interactive=non_interactive
    ):
        ok("Skipping per-agent AWS profiles.")
        return
    state.use_aws = True
    aws_consumers = ["huntress", "gordon"]
    for codename in aws_consumers:
        # Only prompt if this agent is enabled.
        if codename not in {state.codename_for(r) for r in state.enabled_roles}:
            continue
        default_profile = f"{codename}-cron"
        profile = ask(
            f"AWS profile for {codename}?", default_profile, non_interactive=non_interactive
        )
        ident = run(["aws", "--profile", profile, "sts", "get-caller-identity"], timeout=20)
        if ident.returncode != 0:
            warn(f"AWS profile '{profile}' not configured. See docs/AWS_SETUP.md.")
            continue
        state.aws_agent_profiles[codename] = profile
        ok(f"AWS profile for {codename}: {profile}")


def step_5_pick_agents(
    state: WizardState, available: list[str], *, agents_arg: str | None, non_interactive: bool
) -> None:
    step("Pick agents")
    if not available:
        warn("No agent runners discovered in bin/. Did parallel agents land yet?")
        warn("Falling back to the full catalog.")
        available = list(AGENT_CATALOG.keys())
    if agents_arg:
        chosen_codenames = {c.strip() for c in agents_arg.split(",") if c.strip()}
        state.enabled_roles = [r for r in available if AGENT_CATALOG[r][0] in chosen_codenames]
        ok(f"Enabled {len(state.enabled_roles)} agents from --agents.")
        return
    print()
    print("  Available agents (default: all enabled):")
    for role in available:
        codename, desc, _, _ = AGENT_CATALOG[role]
        print(f"    [x] {codename:<20s}, {desc}")
    print()
    if non_interactive:
        state.enabled_roles = list(available)
        ok(f"All {len(available)} agents enabled (non-interactive).")
        return
    raw = ask("Press Enter to accept all, or type comma-separated codenames to TOGGLE OFF", "")
    toggle_off = {c.strip() for c in raw.split(",") if c.strip()}
    state.enabled_roles = [r for r in available if AGENT_CATALOG[r][0] not in toggle_off]
    if not state.enabled_roles:
        warn("Nothing enabled. Re-running selection with all agents on.")
        state.enabled_roles = list(available)
    ok(f"{len(state.enabled_roles)} agents enabled.")


def step_6_codenames(state: WizardState, *, non_interactive: bool) -> None:
    step("Codenames")
    used: set[str] = set()
    for role in state.enabled_roles:
        default, desc, _, _ = AGENT_CATALOG[role]
        while True:
            chosen = ask(
                f"Codename for {desc.split(' (')[0]}?", default, non_interactive=non_interactive
            )
            if not CODENAME_RE.match(chosen):
                fail("Codename must match ^[a-z][a-z0-9-]*$")
                if non_interactive:
                    chosen = default
                    break
                continue
            if chosen in used:
                fail("Codename already used in this fleet.")
                if non_interactive:
                    chosen = default
                    break
                continue
            break
        state.role_to_codename[role] = chosen
        used.add(chosen)
    ok(f"Codenames assigned for {len(state.enabled_roles)} agents.")


def step_7_repos(state: WizardState, *, non_interactive: bool) -> None:
    step("Per-agent repos")
    repo_roles = [r for r in state.enabled_roles if AGENT_CATALOG[r][2]]
    if not repo_roles:
        ok("No repo-operating agents enabled; skipping.")
    for role in repo_roles:
        codename = state.codename_for(role)
        if non_interactive or not state.repos:
            state.role_to_repos[role] = list(state.repos)
            continue
        print()
        print(f"  Repos for {codename} ({AGENT_CATALOG[role][1]}):")
        for i, repo in enumerate(state.repos, 1):
            print(f"    {i:>2}. {repo}")
        raw = ask("Numbers (comma-separated), 'all', or 'engineering' (excludes specs/docs)", "all")
        state.role_to_repos[role] = _resolve_repo_selection(raw, state.repos)
    # Special prompts (Huntress staging URL, Gordon ECS cluster, etc.)
    for role in state.enabled_roles:
        codename = state.codename_for(role)
        # Match by canonical Batman name even if operator renamed the codename.
        canonical = AGENT_CATALOG[role][0]
        prompts = SPECIAL_PROMPTS.get(canonical, [])
        if not prompts:
            continue
        extras: dict[str, str] = {}
        for env_key, label in prompts:
            val = ask(f"{label}", "", non_interactive=non_interactive)
            if val:
                extras[env_key] = val
        if extras:
            state.role_to_extras[role] = extras


def _resolve_repo_selection(raw: str, repos: list[str]) -> list[str]:
    raw = (raw or "").strip().lower()
    if not raw or raw == "all":
        return list(repos)
    if raw == "engineering":
        return [r for r in repos if not any(s in r.lower() for s in ("spec", "doc", "wiki"))]
    out: list[str] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.isdigit():
            idx = int(tok) - 1
            if 0 <= idx < len(repos):
                out.append(repos[idx])
        elif tok in repos:
            out.append(tok)
    return out


def step_8_schedule(state: WizardState, *, non_interactive: bool) -> None:
    step("Schedules")
    for role in state.enabled_roles:
        state.role_to_schedule[role] = AGENT_CATALOG[role][3]
    if non_interactive:
        ok("Sensible defaults assigned.")
        return
    raw = ask("Press 'a' to customize, anything else to accept defaults", "")
    if raw.lower() != "a":
        ok("Sensible defaults assigned.")
        return
    for role in state.enabled_roles:
        codename = state.codename_for(role)
        current = state.role_to_schedule[role]
        new = ask(f"Schedule for {codename}", current)
        state.role_to_schedule[role] = new


def step_9_generate(state: WizardState, *, non_interactive: bool) -> None:
    step("Generate config")
    conf = render_agents_conf(state)
    target = state.repo_root / "launchd" / "agents.conf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(conf)
    ok(f"wrote {target}")
    env_kvs = env_assignments_for(state)
    upsert_alfredrc(state.alfredrc, env_kvs)
    ok(f"updated {state.alfredrc} with {len(env_kvs)} keys")
    print()
    print("--- agents.conf ---")
    print(conf)
    print("-------------------")
    if not non_interactive and not ask_yes_no("Looks good?", True):
        warn("Re-run alfred-init to revise. Existing config left in place.")
        sys.exit(1)


def step_10_deploy(state: WizardState) -> None:
    step("Deploy")
    deploy_path = state.repo_root / "deploy.sh"
    if not deploy_path.exists():
        fail(f"{deploy_path} missing.")
        sys.exit(1)
    cp = run(["bash", str(deploy_path)], capture=False, timeout=300)
    if cp.returncode != 0:
        fail("deploy.sh failed. Re-run after fixing the cause.")
        sys.exit(1)
    ok("deploy.sh OK")


def step_11_doctor(state: WizardState, *, non_interactive: bool) -> bool:
    step("Doctor")
    doctor_path = state.repo_root / "bin" / "doctor.sh"
    if not doctor_path.exists():
        warn("doctor.sh missing; skipping.")
        return True
    cp = run(["bash", str(doctor_path)], capture=False, timeout=600)
    if cp.returncode == 0:
        ok("doctor passed")
        return True
    fail("doctor reported failures.")
    if non_interactive:
        return False
    return ask_yes_no("Continue anyway?", False)


def step_12_smoke(state: WizardState) -> None:
    step("Smoke test")
    n = len(state.enabled_roles)
    if state.slack_webhook:
        ok_post, body = slack_post(
            state.slack_webhook, f"alfred-os: configured and ready. {n} agents enabled."
        )
        if ok_post:
            ok("final Slack post sent")
        else:
            warn(f"Final Slack post failed: {body}")
    print()
    print(f"{STYLE.GREEN}Done.{STYLE.OFF}")
    print()
    print(f"  Fleet: {n} agents enabled")
    if state.slack_webhook:
        masked = state.slack_webhook[:48] + "…"
        print(f"  Slack: {masked}")
    print("  Agents:")
    for role in state.enabled_roles:
        codename = state.codename_for(role)
        desc = AGENT_CATALOG[role][1]
        sched = state.role_to_schedule.get(role, AGENT_CATALOG[role][3])
        repos = state.role_to_repos.get(role, [])
        repo_str = ", ".join(repos[:3]) + (f" (+{len(repos) - 3})" if len(repos) > 3 else "")
        if repos:
            print(f"    {codename:<22s} ({desc.split(' (')[0]:<32s}) → {sched} on {repo_str}")
        else:
            print(f"    {codename:<22s} ({desc.split(' (')[0]:<32s}) → {sched}")
    print()
    print("  Operator commands:")
    print("    alfred agents:         configured agents + runner-gate state")
    print("    alfred enable <agent> , add an opt-in codename to the runner gate")
    print("    alfred disable <agent>, remove a codename from the runner gate")
    print("    bash bin/doctor.sh:    preflight configured Python agents")
    print()
    print("  Read docs/AGENTS.md for the full codename topology.")
    print("  Read INSTALL.md if anything went sideways.")


# ---------------------------------------------------------------------------
# CLI entry point.
# ---------------------------------------------------------------------------


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="alfred-init agent fleet configuration wizard.")
    p.add_argument(
        "--non-interactive", action="store_true", help="Accept all defaults; never prompt."
    )
    p.add_argument("--config", type=Path, default=None, help="JSON file with pre-baked answers.")
    p.add_argument(
        "--agents",
        type=str,
        default=None,
        help="Comma-separated codenames to enable (skips multi-select).",
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Path to the alfred-os checkout (default: parent of this script).",
    )
    return p.parse_args(list(argv) if argv is not None else None)


def load_config(path: Path) -> dict:
    if not path.exists():
        fail(f"--config file {path} not found.")
        sys.exit(1)
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        fail(f"--config file {path} is not valid JSON: {e}")
        sys.exit(1)


def apply_config_overrides(state: WizardState, cfg: dict) -> None:
    """Honor a small set of pre-baked answers from --config."""
    if "gh_org" in cfg:
        state.gh_org = cfg["gh_org"]
    if "slack_webhook" in cfg:
        state.slack_webhook = cfg["slack_webhook"]
    if "slack_storage" in cfg:
        state.slack_storage = cfg["slack_storage"]
    if "use_aws" in cfg:
        state.use_aws = bool(cfg["use_aws"])
    if "aws_agent_profiles" in cfg:
        state.aws_agent_profiles = dict(cfg["aws_agent_profiles"])
    if "agents" in cfg:
        # cfg["agents"] is a list of codenames.
        wanted = set(cfg["agents"])
        state.enabled_roles = [r for r, (cn, _, _, _) in AGENT_CATALOG.items() if cn in wanted]


def main(argv: Iterable[str] | None = None) -> int:
    if os.environ.get("ALFRED_DOCTOR"):
        print("[ALFRED-INIT-DOCTOR-OK]")
        return 0

    args = parse_args(argv)
    non_interactive = args.non_interactive or bool(os.environ.get("ALFRED_NONINTERACTIVE"))

    repo_root = args.repo_root or Path(__file__).resolve().parent.parent
    alfred_home = Path(os.environ.get("ALFRED_HOME") or (Path.home() / ".alfred"))
    alfredrc = Path(os.environ.get("ALFREDRC") or (Path.home() / ".alfredrc"))

    print(f"{STYLE.BLUE}alfred-init{STYLE.OFF} agent fleet configuration.")
    print(f"  Repo:        {repo_root}")
    print(f"  ALFRED_HOME: {alfred_home}")
    print(f"  ~/.alfredrc: {alfredrc}")
    print()

    state = WizardState(alfred_home=alfred_home, alfredrc=alfredrc, repo_root=repo_root)

    if args.config:
        apply_config_overrides(state, load_config(args.config))

    step_0_preflight(state)
    step_1_claude(non_interactive=non_interactive)
    step_2_github(state, non_interactive=non_interactive)
    step_3_slack(state, non_interactive=non_interactive)
    available = discover_agents(repo_root / "bin")
    step_5_pick_agents(state, available, agents_arg=args.agents, non_interactive=non_interactive)
    step_4_aws(state, non_interactive=non_interactive)  # after pick_agents so we know who needs AWS
    step_6_codenames(state, non_interactive=non_interactive)
    step_7_repos(state, non_interactive=non_interactive)
    step_8_schedule(state, non_interactive=non_interactive)
    step_9_generate(state, non_interactive=non_interactive)
    step_10_deploy(state)
    if not step_11_doctor(state, non_interactive=non_interactive):
        fail("Doctor failed. Resolve and re-run `bash bin/doctor.sh`.")
        return 1
    step_12_smoke(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
