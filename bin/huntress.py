#!/usr/bin/env python3
"""Huntress - post-deploy E2E smoke against a staging URL.

Operator drops their own Playwright tests at ${HERMES_HOME}/tests/huntress/
(the working directory the runner shells `npx playwright test` in). The
target URL is read from ALFRED_HUNTRESS_TARGET_URL.

Test-account credentials are fetched from AWS Secrets Manager
(ALFRED_HUNTRESS_SECRET_ID, default "alfred/huntress/test-account") and
exposed to the Playwright process as HUNTRESS_EMAIL / HUNTRESS_PASSWORD.
The tests must be written to read those env vars.

Optional: ALFRED_HUNTRESS_AWS_PROFILE selects an AWS_PROFILE for both
the secret fetch and the screenshot upload. Without it the default
credential chain applies. ALFRED_HUNTRESS_S3_BUCKET, when set, receives
test-failed PNG screenshots for inclusion in Slack failure messages.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")) + "/lib")
from agent_runner import (
    HERMES_HOME,
    WORKSPACE,
    EventLog,
    PreflightFailed,
    PreflightSpec,
    SpendState,
    doctor_mode,
    preflight,
    run,
    slack_post,
    with_lock,
)

AGENT = os.environ.get("AGENT_CODENAME", "huntress")
LAUNCHD_LABEL = os.environ.get("LAUNCHD_LABEL", f"my.fleet.{AGENT}")

HUNTRESS_DIR = Path(
    os.environ.get("ALFRED_HUNTRESS_TESTS_DIR", str(HERMES_HOME / "tests" / "huntress"))
)
TARGET_URL = os.environ.get("ALFRED_HUNTRESS_TARGET_URL", "")
AWS_PROFILE = os.environ.get("ALFRED_HUNTRESS_AWS_PROFILE", "")
SECRET_ID = os.environ.get("ALFRED_HUNTRESS_SECRET_ID", "alfred/huntress/test-account")
REGION = os.environ.get("AWS_REGION", "us-east-1")
S3_BUCKET = os.environ.get("ALFRED_HUNTRESS_S3_BUCKET", "")
ECS_CLUSTER = os.environ.get("ALFRED_HUNTRESS_ECS_CLUSTER", "")
ECS_SERVICES = [
    s.strip() for s in os.environ.get("ALFRED_HUNTRESS_ECS_SERVICES", "").split(",") if s.strip()
]
DEPLOY_REF_REPO = os.environ.get(
    "ALFRED_HUNTRESS_DEPLOY_REF_REPO", ""
)  # local dir under WORKSPACE for SHA reporting

PREFLIGHT = PreflightSpec(
    agent=AGENT,
    bins=["aws", "npx"] if AWS_PROFILE or SECRET_ID else ["npx"],
    aws_profile=AWS_PROFILE or None,
)


def redact_text(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def secure_run_dir(agent: str) -> Path:
    path = Path(tempfile.mkdtemp(prefix=f"{agent}-run-"))
    path.chmod(0o700)
    return path


def _aws(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run aws with stripped env (force the dedicated profile, ignore inherited keys)."""
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
    if AWS_PROFILE:
        env["AWS_PROFILE"] = AWS_PROFILE
    return subprocess.run(
        ["aws", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def main() -> int:
    with_lock(AGENT)

    try:
        preflight(PREFLIGHT)
    except PreflightFailed:
        return 0

    if doctor_mode():
        print(f"[{AGENT.upper()}-DOCTOR-OK]")
        return 0

    if not TARGET_URL:
        print(f"[{AGENT.upper()}-IDLE] no target URL configured (set ALFRED_HUNTRESS_TARGET_URL)")
        return 0
    if not HUNTRESS_DIR.is_dir():
        print(
            f"[{AGENT.upper()}-IDLE] tests dir {HUNTRESS_DIR} not found "
            "(operator: drop Playwright tests there)"
        )
        return 0

    events = EventLog(agent=AGENT)
    events.emit("firing_started")

    spend = SpendState(AGENT)

    # 1. Optional: staging health via ECS describe-services
    if ECS_CLUSTER and ECS_SERVICES:
        svc = _aws(
            [
                "ecs",
                "describe-services",
                "--region",
                REGION,
                "--cluster",
                ECS_CLUSTER,
                "--services",
                *ECS_SERVICES,
                "--query",
                "services[].{name:serviceName,running:runningCount,desired:desiredCount}",
                "--output",
                "json",
            ]
        )
        try:
            services = json.loads(svc.stdout) if svc.stdout else []
        except json.JSONDecodeError:
            services = []
        events.emit("staging_health_checked", services_status=services)
        if not services:
            msg = f"[{AGENT.upper()}-STAGING-NOT-READY] could not query ECS services"
            print(msg)
            slack_post(msg)
            events.emit("firing_complete", outcome="blocked-no-ecs")
            return 0
        for s in services:
            if s.get("running") != s.get("desired"):
                msg = f"[{AGENT.upper()}-STAGING-NOT-READY] {s['name']} running={s['running']} desired={s['desired']}"
                print(msg)
                slack_post(msg)
                events.emit(
                    "firing_complete", outcome="blocked-staging-not-ready", service=s.get("name")
                )
                return 0

    # 2. Fetch test account
    email = ""
    password = ""
    if SECRET_ID:
        sec = _aws(
            [
                "secretsmanager",
                "get-secret-value",
                "--secret-id",
                SECRET_ID,
                "--region",
                REGION,
                "--query",
                "SecretString",
                "--output",
                "text",
            ]
        )
        if sec.returncode != 0:
            msg = (
                f"[{AGENT.upper()}-BLOCKED] cannot fetch test account secret: {sec.stderr.strip()}"
            )
            print(msg)
            slack_post(msg)
            events.emit("firing_complete", outcome="blocked-secret-fetch")
            return 0
        try:
            creds = json.loads(sec.stdout)
            email = creds["email"]
            password = creds["password"]
        except (json.JSONDecodeError, KeyError) as e:
            msg = f"[{AGENT.upper()}-BLOCKED] malformed test account secret: {e}"
            print(msg)
            slack_post(msg)
            events.emit("firing_complete", outcome="blocked-bad-secret")
            return 0

    # 3. Run Playwright
    ts = int(time.time())
    run_dir = secure_run_dir(AGENT)

    pw_env = dict(os.environ)
    if email:
        pw_env["HUNTRESS_EMAIL"] = email
    if password:
        pw_env["HUNTRESS_PASSWORD"] = password
    pw_env["HUNTRESS_BASE_URL"] = TARGET_URL
    pw_env["HUNTRESS_RUN_DIR"] = str(run_dir)

    pw_started = time.time()
    pw = subprocess.run(
        ["npx", "playwright", "test", "--reporter=json"],
        cwd=str(HUNTRESS_DIR),
        env=pw_env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    pw_duration_ms = int((time.time() - pw_started) * 1000)
    secrets = [
        email,
        password,
        os.environ.get("HUNTRESS_EMAIL", ""),
        os.environ.get("HUNTRESS_PASSWORD", ""),
    ]
    redacted_stdout = redact_text(pw.stdout or "", secrets)
    redacted_stderr = redact_text(pw.stderr or "", secrets)
    (run_dir / "stdout.json").write_text(redacted_stdout)
    (run_dir / "stderr.log").write_text(redacted_stderr)

    spend.increment(firings_today=1)
    events.emit("playwright_done", returncode=pw.returncode, duration_ms=pw_duration_ms)

    if pw.returncode == 0:
        spend.increment(successes_today=1)
        spend.set(consecutive_failures=0)
        print("[SILENT]")
        events.emit("firing_complete", outcome="green")
        return 0

    # Anything past this point is some flavour of Playwright non-zero exit —
    # parse-failed, regression, or selector drift.
    spend.increment(failures_today=1, consecutive_failures=1)

    try:
        report = json.loads(redacted_stdout) if redacted_stdout else {}
    except json.JSONDecodeError:
        report = {}

    # Playwright nests suites recursively; walk the tree.
    failed_titles: list[str] = []
    failed_errors: list[str] = []

    def _walk(suite: dict[str, Any]) -> None:
        for spec in suite.get("specs", []) or []:
            for t in spec.get("tests", []) or []:
                if t.get("status") in ("passed", "skipped"):
                    continue
                title = spec.get("title", "(unnamed spec)")
                results = t.get("results") or []
                first_err = ""
                if results:
                    errs = results[0].get("errors") or []
                    if errs:
                        first_err = (errs[0].get("message") or "").splitlines()[0][:180]
                failed_titles.append(title)
                if first_err:
                    failed_errors.append(f"{title}: {first_err}")
        for child in suite.get("suites", []) or []:
            _walk(child)

    for suite in report.get("suites", []) or []:
        _walk(suite)

    if not failed_titles:
        # No parseable failures = Playwright crashed before producing JSON.
        # Surface stderr/stdout tails so the operator can grab the full log.
        stderr_tail = "\n".join(redacted_stderr.splitlines()[-25:])
        stdout_tail = "\n".join(redacted_stdout.splitlines()[-15:])
        msg_lines = [
            f"❌ {AGENT.title()}: Playwright exited {pw.returncode} with no parseable failures.",
            f"  Run dir (full logs): `{run_dir}/`  (stderr.log + stdout.json)",
        ]
        if stderr_tail.strip():
            excerpt = stderr_tail[-1200:]
            msg_lines.append(f"  Stderr tail:\n```\n{excerpt}\n```")
        if stdout_tail.strip() and not redacted_stdout.lstrip().startswith("{"):
            msg_lines.append(f"  Stdout tail:\n```\n{stdout_tail[-600:]}\n```")
        msg = "\n".join(msg_lines)
        print(msg)
        slack_post(msg)
        events.emit(
            "firing_complete",
            outcome="blocked-unparseable",
            stderr_bytes=len(redacted_stderr),
            stdout_bytes=len(redacted_stdout),
            run_dir=str(run_dir),
        )
        return 0

    # Optional screenshot upload to S3
    slack_lines = []
    if S3_BUCKET:
        pngs = list(run_dir.rglob("test-failed-*.png"))[:6]
        for png in pngs:
            rel = png.parent.name
            s3_key = f"{AGENT}-failures/{ts}/{rel}.png"
            cp = _aws(
                [
                    "s3",
                    "cp",
                    str(png),
                    f"s3://{S3_BUCKET}/{s3_key}",
                ],
                timeout=30,
            )
            if cp.returncode != 0:
                continue
            presigned = _aws(
                [
                    "s3",
                    "presign",
                    f"s3://{S3_BUCKET}/{s3_key}",
                    "--expires-in",
                    "3600",
                ],
                timeout=15,
            ).stdout.strip()
            slack_lines.append(f"❌ {AGENT.title()}: {rel} ({presigned})")

    # "Drift" = selector / routing change rather than a backend regression.
    is_drift = "TimeoutError" in redacted_stderr or any("TimeoutError" in e for e in failed_errors)

    # Optional: report deploy SHA when a reference repo is configured
    deploy_sha = "unknown"
    if DEPLOY_REF_REPO:
        sha = run(
            ["git", "-C", str(WORKSPACE / DEPLOY_REF_REPO), "rev-parse", "--short", "origin/main"],
            timeout=10,
        ).stdout.strip()
        if sha:
            deploy_sha = sha

    error_block = ""
    if failed_errors:
        error_block = "\n*Failures:*\n" + "\n".join(f"  • {e}" for e in failed_errors[:6])

    if not is_drift:
        msg = (
            "\n".join(slack_lines)
            + error_block
            + f"\nReal regression at deploy SHA {deploy_sha}. Manual investigation required."
            + f"\nFull logs: `{run_dir}/`"
        )
        print(msg)
        slack_post(msg)
        events.emit(
            "firing_complete",
            outcome="regression",
            failed_count=len(failed_titles),
            deploy_sha=deploy_sha,
        )
        return 0

    msg = (
        "\n".join(slack_lines)
        + error_block
        + f"\nLikely selector drift (TimeoutError on click). Deploy SHA {deploy_sha}."
        + f"\nFull logs: `{run_dir}/`"
    )
    print(msg)
    slack_post(msg)
    events.emit(
        "firing_complete", outcome="drift", failed_count=len(failed_titles), deploy_sha=deploy_sha
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
