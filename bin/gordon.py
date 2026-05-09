#!/usr/bin/env python3
"""gordon - deployment-health agent.

Daily check that what's running on staging matches what's on `main`,
plus an optional Sentry error-rate snapshot. Codename: Commissioner Gordon.

Scope (deliberately narrow - fast and cheap):

1. For every ECS service in the configured staging cluster, read the live
   task definition's container image. Compare the image tag against the
   newest commit on the corresponding GitHub repo's main. Report any
   service whose live image SHA is older than main HEAD.
2. Optional: pull the top 5 Sentry issues by event count over the last 24h
   via the Sentry API. Surface URL + title + count.
3. Post a single Slack summary if anything's off; otherwise stay quiet.

Configuration env vars:
  ALFRED_GORDON_ECS_CLUSTER     ECS cluster name to query (required)
  ALFRED_GORDON_AWS_PROFILE     AWS_PROFILE override (optional)
  ALFRED_GORDON_SERVICES        comma-separated 'service-name=org/repo:branch'
                                e.g. 'staging-api=myorg/backend:main,staging-fe=myorg/frontend:main'
  ALFRED_GORDON_SENTRY_ORG      Sentry org slug (optional - skips Sentry section if unset)
  ALFRED_GORDON_SENTRY_SECRET_ID  AWS Secrets Manager secret with auth_token
                                  (default: alfred/sentry-api-token)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

sys.path.insert(0, os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")) + "/lib")
from agent_runner import (
    EventLog,
    PreflightFailed,
    PreflightSpec,
    SpendState,
    doctor_mode,
    gh_json,
    preflight,
    slack_post,
    with_lock,
)

AGENT = os.environ.get("AGENT_CODENAME", "gordon")
LAUNCHD_LABEL = os.environ.get("LAUNCHD_LABEL", f"my.fleet.{AGENT}")

AWS_PROFILE = os.environ.get("ALFRED_GORDON_AWS_PROFILE", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")
STAGING_CLUSTER = os.environ.get("ALFRED_GORDON_ECS_CLUSTER", "")
SENTRY_ORG = os.environ.get("ALFRED_GORDON_SENTRY_ORG", "")
SENTRY_SECRET_ID = os.environ.get("ALFRED_GORDON_SENTRY_SECRET_ID", "alfred/sentry-api-token")


def _parse_services_env() -> dict[str, tuple[str, str]]:
    """Parse 'svc1=org/repo1:main,svc2=org/repo2:main' into
    {svc1: (org/repo1, main), svc2: (org/repo2, main)}."""
    out: dict[str, tuple[str, str]] = {}
    raw = os.environ.get("ALFRED_GORDON_SERVICES", "").strip()
    if not raw:
        return out
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        svc, target = entry.split("=", 1)
        if ":" in target:
            slug, branch = target.rsplit(":", 1)
        else:
            slug, branch = target, "main"
        out[svc.strip()] = (slug.strip(), branch.strip())
    return out


SERVICE_TO_REPO = _parse_services_env()

PREFLIGHT = PreflightSpec(
    agent=AGENT,
    bins=["aws", "gh"],
    aws_profile=AWS_PROFILE or None,
    require_gh_auth=True,
)


class MonitoringFetchError(RuntimeError):
    """Raised when a monitoring input cannot be collected safely."""


def _aws(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
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


def _aws_json(args: list[str], timeout: int = 30) -> Any:
    res = _aws(args, timeout=timeout)
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip().splitlines()
        detail = err[-1] if err else "no output"
        raise MonitoringFetchError(f"aws {' '.join(args[:2])} failed: {detail[:160]}")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise MonitoringFetchError(f"aws {' '.join(args[:2])} returned invalid JSON: {e}") from e


def _image_sha_from_tag(image: str) -> str | None:
    """Pull the 7-40 char hex SHA from an image tag.

    Examples:
      <account>.dkr.ecr.us-east-1.amazonaws.com/myrepo:abc123def
      ghcr.io/foo/bar:sha-abc123def4567
    """
    if "@sha256:" in image:
        return image.split("@sha256:")[1][:12]
    tag = image.rsplit(":", 1)[-1] if ":" in image else ""
    m = re.search(r"\b([0-9a-f]{7,40})\b", tag)
    return m.group(1) if m else None


def check_ecs_drift() -> list[dict[str, Any]]:
    """Return one record per service. drift=True when live SHA != main HEAD."""
    if not (STAGING_CLUSTER and SERVICE_TO_REPO):
        return []
    services = list(SERVICE_TO_REPO.keys())
    desc = _aws_json(
        [
            "ecs",
            "describe-services",
            "--cluster",
            STAGING_CLUSTER,
            "--services",
            *services,
            "--region",
            REGION,
        ]
    )
    out: list[dict[str, Any]] = []
    if not desc:
        raise MonitoringFetchError("ecs describe-services returned no data")
    failures = desc.get("failures") or []
    if failures:
        reason = "; ".join(
            f"{f.get('arn') or f.get('reason') or 'unknown'}:{f.get('reason') or 'failed'}"
            for f in failures
        )
        raise MonitoringFetchError(f"ecs describe-services returned failures: {reason[:180]}")

    returned_services = {svc.get("serviceName") for svc in desc.get("services", [])}
    missing = sorted(set(services) - {name for name in returned_services if name})
    if missing:
        raise MonitoringFetchError(
            "ecs describe-services missing service(s): " + ", ".join(missing)
        )

    for svc in desc.get("services", []):
        name = svc.get("serviceName")
        td_arn = svc.get("taskDefinition")
        if not name or not td_arn:
            continue
        td = _aws_json(
            [
                "ecs",
                "describe-task-definition",
                "--task-definition",
                td_arn,
                "--region",
                REGION,
            ]
        )
        live_image = ""
        containers = (td.get("taskDefinition") or {}).get("containerDefinitions") or []
        if containers:
            live_image = containers[0].get("image", "")
        live_sha = _image_sha_from_tag(live_image) or ""

        repo_slug, branch = SERVICE_TO_REPO[name]
        # NOTE: don't use `--jq .sha` here — gh_json json-parses stdout, and a
        # bare SHA is not valid JSON, so it would be silently dropped. Fetch
        # the full commit object and pluck .sha ourselves.
        commit = gh_json(
            ["gh", "api", f"/repos/{repo_slug}/commits/{branch}"],
            default={},
        )
        head_sha = ""
        if isinstance(commit, dict):
            head_sha = (commit.get("sha") or "")[:12]
        if not head_sha:
            raise MonitoringFetchError(f"gh commit fetch failed for {repo_slug}@{branch}")

        a = (live_sha or "").lower()[:12]
        b = (head_sha or "").lower()[:12]
        in_sync = bool(a and b and a == b)
        out.append(
            {
                "service": name,
                "repo": repo_slug,
                "live_image": live_image,
                "live_sha": live_sha,
                "main_sha": head_sha,
                "in_sync": in_sync,
            }
        )
    return out


def fetch_sentry_token() -> str | None:
    if not SENTRY_ORG:
        return None
    res = _aws_json(
        [
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            SENTRY_SECRET_ID,
            "--region",
            REGION,
        ]
    )
    if not res:
        return None
    secret_str = res.get("SecretString") or "{}"
    try:
        return json.loads(secret_str).get("auth_token")
    except json.JSONDecodeError:
        return None


def fetch_sentry_top_issues(token: str, hours: int = 24, limit: int = 5) -> list[dict[str, Any]]:
    url = (
        f"https://sentry.io/api/0/organizations/{SENTRY_ORG}/issues/"
        f"?statsPeriod={hours}h&limit={limit}&sort=freq&query=is:unresolved"
    )
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "User-Agent": f"alfred-{AGENT}/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        raise MonitoringFetchError(f"sentry fetch failed: {e}") from e
    out: list[dict[str, Any]] = []
    for issue in data[:limit] if isinstance(data, list) else []:
        out.append(
            {
                "id": issue.get("shortId") or issue.get("id"),
                "title": (issue.get("title") or "")[:120],
                "count": issue.get("count"),
                "users": (issue.get("userCount") or 0),
                "permalink": issue.get("permalink"),
            }
        )
    return out


def main() -> int:
    with_lock(AGENT)

    try:
        preflight(PREFLIGHT)
    except PreflightFailed:
        return 0

    if doctor_mode():
        print(f"[{AGENT.upper()}-DOCTOR-OK]")
        return 0

    if not STAGING_CLUSTER:
        print(f"[{AGENT.upper()}-IDLE] no ECS cluster configured (set ALFRED_GORDON_ECS_CLUSTER)")
        return 0

    events = EventLog(agent=AGENT)
    events.emit("firing_started")
    spend = SpendState(AGENT)

    blocking_errors: list[str] = []
    optional_errors: list[str] = []
    try:
        drift = check_ecs_drift()
    except MonitoringFetchError as e:
        drift = []
        blocking_errors.append(str(e))
    drifted = [r for r in drift if not r["in_sync"]]
    events.emit("ecs_drift_checked", services=len(drift), drifted=len(drifted))

    sentry_issues: list[dict[str, Any]] = []
    try:
        token = fetch_sentry_token()
    except MonitoringFetchError as e:
        token = None
        optional_errors.append(str(e))
    try:
        if token:
            sentry_issues = fetch_sentry_top_issues(token, hours=24, limit=5)
    except MonitoringFetchError as e:
        optional_errors.append(str(e))
    events.emit("sentry_fetched", count=len(sentry_issues), token_available=bool(token))

    spend.increment(firings_today=1)

    if blocking_errors:
        spend.increment(failures_today=1, consecutive_failures=1)
        msg = f"⚠️ {AGENT.title()}: monitoring input collection failed: " + "; ".join(
            blocking_errors[:3]
        )
        print(msg)
        slack_post(msg, severity="alert")
        events.emit("firing_complete", outcome="monitoring-fetch-failed")
        return 0

    summary_lines = [
        f"[{AGENT.upper()}-OK] services={len(drift)} drifted={len(drifted)} sentry_issues={len(sentry_issues)}"
    ]
    print("\n".join(summary_lines))

    if not drifted and not sentry_issues:
        if optional_errors:
            spend.increment(failures_today=1, consecutive_failures=1)
            msg = f"⚠️ {AGENT.title()}: optional Sentry collection failed: " + "; ".join(
                optional_errors[:3]
            )
            print(msg)
            slack_post(msg, severity="warn")
            events.emit("firing_complete", outcome="sentry-fetch-failed")
            return 0
        # Healthy day, stay quiet on Slack.
        return 0

    slack_lines = [f"*{AGENT.title()} - staging deployment health*"]
    if drifted:
        slack_lines.append("\n*ECS drift (live ≠ main):*")
        for r in drifted:
            slack_lines.append(
                f"  • `{r['service']}` ({r['repo']}): live=`{r['live_sha'] or '?'}` "
                f"main=`{r['main_sha'] or '?'}`"
            )
    elif drift:
        slack_lines.append(f"\nECS drift: ✅ all {len(drift)} services in sync with main")

    if sentry_issues:
        slack_lines.append("\n*Sentry - top 5 unresolved issues (24h):*")
        for issue in sentry_issues:
            link = issue.get("permalink") or "(no link)"
            slack_lines.append(
                f"  • {issue['count']}x {issue['users']} users - `{issue['id']}` {issue['title']}\n    <{link}>"
            )

    if optional_errors:
        slack_lines.append("\n*Sentry collection warning:*")
        for error in optional_errors[:3]:
            slack_lines.append(f"  • {error}")

    msg = "\n".join(slack_lines)
    severity = "alert" if drifted else "info"
    slack_post(msg, severity=severity)
    events.emit("firing_done", drifted=len(drifted), sentry=len(sentry_issues))
    return 0


if __name__ == "__main__":
    sys.exit(main())
