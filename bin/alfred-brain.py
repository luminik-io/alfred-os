#!/usr/bin/env python3
"""``alfred brain`` - operator CLI for the fleet-brain memory layer.

Subcommands:

    alfred-brain.py status
        Print row counts, db path, and schema version.

    alfred-brain.py lessons <codename> <repo> [--query Q] [--limit N]
        List recall-able lessons most-recent first. Either positional
        may be ``-`` to widen the scope (e.g. all lessons for one repo
        across every codename: ``alfred-brain.py lessons - your-org/api``).

    alfred-brain.py reflect <codename> <repo> <body>
        Manually file a lesson from the shell. Useful for seeding the
        brain with operator knowledge before any agent has fired.

    alfred-brain.py propose <codename> <repo> <body>
        Stage a memory candidate for later review.

    alfred-brain.py candidates [--status candidate|validated|rejected|retired|all]
        List staged memory candidates.

    alfred-brain.py promote <candidate-id>
        Turn a reviewed candidate into a trusted lesson.

    alfred-brain.py reject <candidate-id>
        Mark a staged candidate as rejected.

    alfred-brain.py failures
        List normalized non-success events.

    alfred-brain.py doctor
        Read-only health summary for the memory layer.

    alfred-brain.py harvest [--apply]
        Propose reviewable memory candidates from repeated failure patterns.

    alfred-brain.py redis-status
        Check an optional Redis Agent Memory Server endpoint.

    alfred-brain.py redis-sync
        Mirror reviewed fleet-brain lessons into Redis AMS.

    alfred-brain.py firings [--codename C] [--status S] [--limit N]
        List firing audit rows.

    alfred-brain.py files <repo> [--codename C] [--path P] [--limit N]
        List recent files the fleet touched in a repo.

    alfred-brain.py forget <id>
        Delete one lesson by id. Use ``alfred-brain.py forget --before 30d``
        to GC anything older than 30 days.

    alfred-brain.py export [--out PATH]
        Write a JSON snapshot to PATH (default: stdout).

Pure stdlib. Core fleet-brain commands are local-only and write only inside
``$ALFRED_FLEET_BRAIN_DB`` / ``$ALFRED_HOME``. The Redis subcommands are the
exception: they call the configured Redis Agent Memory Server endpoint because
that bridge is explicit and optional.

The wrapper ``bin/alfred`` exposes this as ``alfred brain status``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Resolve lib/ relative to this script regardless of how it was invoked.
_HERE = Path(__file__).resolve().parent
for candidate in (
    _HERE.parent / "lib",
    Path(os.environ.get("ALFRED_HOME", "")) / "lib",
):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from fleet_brain import FleetBrain, default_db_path  # noqa: E402
from fleet_brain.doctor import run_memory_doctor  # noqa: E402
from memory.redis_agent_memory import RedisAgentMemoryProvider  # noqa: E402


def _build_brain(args: argparse.Namespace) -> FleetBrain:
    db_path = args.db or os.environ.get("ALFRED_FLEET_BRAIN_DB")
    return FleetBrain(db_path=db_path) if db_path else FleetBrain()


def cmd_status(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    s = brain.stats()
    db_path = args.db or os.environ.get("ALFRED_FLEET_BRAIN_DB") or str(default_db_path())
    print(f"alfred-brain: db = {db_path}")
    print(f"  lessons     {s['lessons']}")
    print(f"  firings     {s['firings']}")
    print(f"  file_touches {s['file_touches']}")
    print(f"  candidates  {s['memory_candidates']} ({s['memory_candidates_open']} open)")
    print(f"  failures    {s['failure_events']}")
    print(f"  github      {s['github_items']}")
    print(f"  bundles     {s['bundle_items']}")
    print(f"  workers     {s['worker_heartbeats']} ({s['workers_running']} running)")
    print(f"  repo_notes  {s['repo_notes']}")
    print(f"  tags        {s['tags']}")
    print(f"  codenames   {s['codenames']}")
    print(f"  repos       {s['repos']}")
    return 0


def cmd_lessons(args: argparse.Namespace) -> int:
    codename = None if args.codename == "-" else args.codename
    repo = None if args.repo == "-" else args.repo
    brain = _build_brain(args)
    lessons = brain.recall(codename=codename, repo=repo, query=args.query, limit=args.limit)
    if args.json:
        payload = [
            {
                "id": L.id,
                "codename": L.codename,
                "repo": L.repo,
                "body": L.body,
                "tags": L.tags,
                "severity": L.severity,
                "firing_id": L.firing_id,
                "created_at": L.created_at.astimezone(UTC).isoformat(),
            }
            for L in lessons
        ]
        print(json.dumps(payload, indent=2))
        return 0
    if not lessons:
        print("alfred-brain: no lessons match", file=sys.stderr)
        return 0
    for L in lessons:
        ts = L.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        tag_str = ("[" + ",".join(L.tags) + "] ") if L.tags else ""
        sev_str = "" if L.severity == "info" else f"({L.severity}) "
        print(f"{L.id}  {ts}  {L.codename}/{L.repo}")
        print(f"  {sev_str}{tag_str}{L.body}")
    return 0


def cmd_reflect(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    tags = [t.strip() for t in (args.tag or [])]
    if args.candidate:
        candidate = brain.propose_memory(
            codename=args.codename,
            repo=args.repo,
            body=args.body,
            tags=tags,
            severity=args.severity,
            source="manual",
            source_firing_id=args.firing_id,
            confidence=args.confidence,
        )
        print(f"alfred-brain: proposed candidate {candidate.id}")
        return 0
    lesson = brain.reflect(
        codename=args.codename,
        repo=args.repo,
        body=args.body,
        tags=tags,
        severity=args.severity,
        firing_id=args.firing_id,
    )
    print(f"alfred-brain: reflected lesson {lesson.id}")
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    candidate = brain.propose_memory(
        codename=args.codename,
        repo=args.repo,
        body=args.body,
        tags=[t.strip() for t in (args.tag or [])],
        severity=args.severity,
        source=args.source,
        source_firing_id=args.firing_id,
        evidence=args.evidence or "",
        confidence=args.confidence,
    )
    if args.json:
        print(json.dumps(_candidate_to_dict(candidate), indent=2))
    else:
        print(f"alfred-brain: proposed candidate {candidate.id}")
    return 0


def cmd_candidates(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    status = None if args.status == "all" else args.status
    candidates = brain.list_memory_candidates(
        status=status,
        repo=args.repo,
        codename=args.codename,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps([_candidate_to_dict(C) for C in candidates], indent=2))
        return 0
    if not candidates:
        print("alfred-brain: no memory candidates match", file=sys.stderr)
        return 0
    for C in candidates:
        created = C.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        tags = (" [" + ",".join(C.tags) + "]") if C.tags else ""
        reviewed = f" reviewed_by={C.reviewed_by}" if C.reviewed_by else ""
        print(
            f"{C.id}  {created}  {C.status}  {C.codename}/{C.repo} "
            f"severity={C.severity} confidence={C.confidence:.2f}{reviewed}"
        )
        print(f"  source={C.source}{tags}")
        print(f"  {C.body}")
        if C.evidence:
            print(f"  evidence: {C.evidence}")
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    try:
        lesson = brain.promote_memory_candidate(
            args.id,
            reviewer=args.reviewer,
            review_note=args.note or "",
        )
    except ValueError as exc:
        print(f"alfred-brain: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps(
                {
                    "candidate_id": args.id,
                    "lesson_id": lesson.id,
                    "codename": lesson.codename,
                    "repo": lesson.repo,
                },
                indent=2,
            )
        )
    else:
        print(f"alfred-brain: promoted {args.id} -> lesson {lesson.id}")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    try:
        candidate = brain.reject_memory_candidate(
            args.id,
            reviewer=args.reviewer,
            review_note=args.note or "",
        )
    except ValueError as exc:
        print(f"alfred-brain: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(_candidate_to_dict(candidate), indent=2))
    else:
        print(f"alfred-brain: rejected {args.id}")
    return 0


def cmd_firings(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    firings = brain.list_firings(
        codename=args.codename,
        status=args.status,
        limit=args.limit,
    )
    if not firings:
        print("alfred-brain: no firings recorded", file=sys.stderr)
        return 0
    for F in firings:
        started = F.started_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        repo_str = f" {F.repo}" if F.repo else ""
        pr_str = f" {F.pr_url}" if F.pr_url else ""
        print(f"{F.firing_id}  {started}  {F.codename}{repo_str}  status={F.status}{pr_str}")
        if F.summary:
            print(f"  {F.summary}")
    return 0


def cmd_files(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    touches = brain.list_file_touches(
        repo=None if args.repo == "-" else args.repo,
        codename=args.codename,
        path=args.path,
        limit=args.limit,
    )
    if args.json:
        payload = [
            {
                "id": T.id,
                "repo": T.repo,
                "path": T.path,
                "codename": T.codename,
                "firing_id": T.firing_id,
                "pr_url": T.pr_url,
                "change_type": T.change_type,
                "touched_at": T.touched_at.astimezone(UTC).isoformat(),
            }
            for T in touches
        ]
        print(json.dumps(payload, indent=2))
        return 0
    if not touches:
        print("alfred-brain: no file touches recorded", file=sys.stderr)
        return 0
    for T in touches:
        touched = T.touched_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        pr_str = f" {T.pr_url}" if T.pr_url else ""
        firing_str = f" firing={T.firing_id}" if T.firing_id else ""
        print(f"{touched}  {T.codename}/{T.repo}  {T.change_type}  {T.path}{firing_str}{pr_str}")
    return 0


def cmd_failures(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    failures = brain.list_failures(
        repo=args.repo,
        codename=args.codename,
        subtype=args.subtype,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps([_failure_to_dict(F) for F in failures], indent=2))
        return 0
    if not failures:
        print("alfred-brain: no failures recorded", file=sys.stderr)
        return 0
    for F in failures:
        created = F.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        repo = f" {F.repo}" if F.repo else ""
        engine = f" engine={F.engine}" if F.engine else ""
        firing = f" firing={F.firing_id}" if F.firing_id else ""
        print(
            f"{F.id}  {created}  {F.codename}{repo}  subtype={F.subtype} "
            f"severity={F.severity}{engine}{firing}"
        )
        if F.summary:
            print(f"  {F.summary}")
    return 0


def cmd_failure_patterns(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    patterns = brain.list_failure_patterns(
        repo=args.repo,
        codename=args.codename,
        window_days=args.window_days,
        min_count=args.min_count,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(patterns, indent=2))
        return 0
    if not patterns:
        print("alfred-brain: no repeated failure patterns", file=sys.stderr)
        return 0
    for pattern in patterns:
        repo = f" repo={pattern['repo']}" if pattern.get("repo") else ""
        engine = f" engine={pattern['engine']}" if pattern.get("engine") else ""
        print(
            f"{pattern['codename']}{repo} subtype={pattern['subtype']}{engine} "
            f"count={pattern['count']} class={pattern['classification']} "
            f"action={pattern['suggested_action']}"
        )
        if pattern.get("latest_summary"):
            print(f"  {pattern['latest_summary']}")
    return 0


def cmd_governor(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    report = brain.reliability_report(
        window_days=args.window_days,
        failure_min_count=args.min_count,
        stale_worker_minutes=args.stale_worker_minutes,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(report, indent=2))
        return 1 if report["status"] == "fail" else 0
    print(f"alfred-brain governor: {report['status']}")
    actions = report.get("actions") or []
    if not actions:
        print("  ok   no reliability actions")
        return 0
    for action in actions:
        print(f"  {action['severity']:7} {action['kind']}: {action['summary']}")
    return 1 if report["status"] == "fail" else 0


def cmd_github(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    items = brain.list_github_items(
        repo=args.repo,
        kind=args.kind,
        state=args.state,
        bundle_slug=args.bundle,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps([_github_item_to_dict(item) for item in items], indent=2))
        return 0
    if not items:
        print("alfred-brain: no GitHub items cached", file=sys.stderr)
        return 0
    for item in items:
        updated = item.updated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        bundle = f" bundle={item.bundle_slug}" if item.bundle_slug else ""
        print(
            f"{updated}  {item.repo}#{item.number}  {item.kind}/{item.state}{bundle}  {item.title}"
        )
        if item.url:
            print(f"  {item.url}")
    return 0


def cmd_bundles(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    items = brain.list_bundle_items(
        bundle_slug=args.bundle,
        state=args.state,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps([_bundle_item_to_dict(item) for item in items], indent=2))
        return 0
    if not items:
        print("alfred-brain: no bundle items cached", file=sys.stderr)
        return 0
    for item in items:
        updated = item.updated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        print(
            f"{updated}  bundle={item.bundle_slug}  {item.repo}#{item.number}  "
            f"{item.item_kind}/{item.state}  {item.title}"
        )
    return 0


def cmd_workers(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    if args.stale:
        workers = brain.list_stale_workers(max_age_minutes=args.max_age_minutes)
    else:
        workers = brain.list_worker_heartbeats(
            codename=args.codename,
            status=args.status,
            limit=args.limit,
        )
    if args.json:
        print(json.dumps([_worker_to_dict(worker) for worker in workers], indent=2))
        return 0
    if not workers:
        print("alfred-brain: no worker heartbeats match", file=sys.stderr)
        return 0
    now = datetime.now(UTC)
    for worker in workers:
        age_m = int((now - worker.heartbeat_at.astimezone(UTC)).total_seconds() // 60)
        repo = f" repo={worker.repo}" if worker.repo else ""
        pid = f" pid={worker.pid}" if worker.pid is not None else ""
        print(
            f"{worker.codename} firing={worker.firing_id} status={worker.status} "
            f"age={max(age_m, 0)}m{repo}{pid}"
        )
        if worker.detail:
            print(f"  {worker.detail}")
    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    worker = brain.upsert_worker_heartbeat(
        codename=args.codename,
        firing_id=args.firing_id,
        status=args.status,
        repo=args.repo,
        pid=args.pid,
        detail=args.detail or "",
    )
    if args.json:
        print(json.dumps(_worker_to_dict(worker), indent=2))
    else:
        print(f"alfred-brain: heartbeat {worker.codename}/{worker.firing_id} {worker.status}")
    return 0


def cmd_promotions(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    suggestions = brain.suggest_memory_promotions(
        min_confidence=args.min_confidence,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(suggestions, indent=2))
        return 0
    if not suggestions:
        print("alfred-brain: no promotion suggestions", file=sys.stderr)
        return 0
    for item in suggestions:
        reasons = ", ".join(item["reasons"])
        print(
            f"{item['candidate_id']}  score={item['score']:.2f}  "
            f"{item['codename']}/{item['repo']}  {reasons}"
        )
        print(f"  {item['body']}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    report = run_memory_doctor(args.db or os.environ.get("ALFRED_FLEET_BRAIN_DB"))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"alfred-brain doctor: {report['status']}")
        for check in report["checks"]:
            print(f"  {check['status']:4} {check['name']}: {check['detail']}")
    return 1 if report["status"] == "fail" else 0


def cmd_harvest(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    proposals = _harvest_failure_pattern_memories(
        brain,
        window_days=args.window_days,
        min_count=args.min_count,
        limit=args.limit,
        apply=bool(args.apply),
    )
    payload = {
        "applied": bool(args.apply),
        "proposals": proposals,
        "queued": sum(1 for item in proposals if item["status"] == "queued"),
        "duplicates": sum(1 for item in proposals if item["status"] == "duplicate"),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    title = "harvest queued" if args.apply else "harvest preview"
    print(
        f"alfred-brain: {title} {len(proposals)} failure-pattern candidate(s) "
        f"({payload['duplicates']} duplicate)"
    )
    for item in proposals:
        candidate = f" id={item['candidate_id']}" if item.get("candidate_id") else ""
        print(f"  {item['status']}{candidate} {item['codename']}/{item['repo']}")
        print(f"    {item['body']}")
    if not args.apply:
        print("  run with --apply to queue reviewable memory candidates")
    return 0


def cmd_auto_promote(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    summary = brain.auto_promote_candidates(
        threshold=args.threshold,
        max_per_run=args.max_per_run,
    )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    if not summary["enabled"]:
        print(
            "alfred-brain: auto-promotion is disarmed. Set ALFRED_AUTO_PROMOTE=1 "
            "to arm it (ALFRED_AUTO_PROMOTE_KILL=1 overrides).",
            file=sys.stderr,
        )
        return 0
    promoted = summary["promoted"]
    print(
        f"alfred-brain: auto-promote considered {summary['considered']} candidate(s), "
        f"promoted {len(promoted)} "
        f"(threshold={summary['threshold']:.2f}, cap={summary['cap']})"
    )
    if promoted:
        print(f"  promoted: {', '.join(promoted)}")
    print(
        "  held for human: "
        f"behavior-change={summary['flagged_behavior_change']}, "
        f"duplicate={summary['skipped_duplicate']}, "
        f"judge-lowered={summary['held_low_confidence']}, "
        f"already-held={summary['skipped_flagged']}"
    )
    print(
        "  skipped: "
        f"low-confidence={summary['skipped_low_confidence']}, "
        f"no-evidence={summary['skipped_no_evidence']}, "
        f"conflict={summary['skipped_conflict']}"
    )
    if summary["judge_enabled"]:
        budget = ", budget-exhausted" if summary["judge_budget_exhausted"] else ""
        print(f"  judge: calls={summary['judge_calls']}, errors={summary['judge_errors']}{budget}")
    return 0


def _build_redis_provider() -> RedisAgentMemoryProvider:
    return RedisAgentMemoryProvider.from_env()


def cmd_redis_status(args: argparse.Namespace) -> int:
    provider = _build_redis_provider()
    health = provider.health()
    if args.json:
        print(json.dumps(health, indent=2))
        return 0 if health.get("ok") else 1
    if health.get("ok"):
        print(f"alfred-brain redis: ok {health['base_url']} namespace={health['namespace']}")
        response = health.get("response")
        if isinstance(response, dict):
            detail = response.get("status") or response.get("version") or response.get("service")
            if detail:
                print(f"  server {detail}")
        return 0
    print(
        f"alfred-brain redis: unavailable {health['base_url']} namespace={health['namespace']}",
        file=sys.stderr,
    )
    print(f"  {health.get('error', 'unknown error')}", file=sys.stderr)
    return 1


def cmd_ams_status(args: argparse.Namespace) -> int:
    from memory.ams_server import AmsServerConfig

    cfg = AmsServerConfig.from_env()
    provider = _build_redis_provider()
    health = provider.health()
    provider_base_url = str(getattr(provider, "base_url", health.get("base_url", cfg.base_url)))
    health_url = f"{provider_base_url}/v1/health"
    payload = {
        "base_url": provider_base_url,
        "health_url": health_url,
        "server_base_url": cfg.base_url,
        "redis_url": cfg.redis_url,
        "auth_mode": cfg.auth_mode,
        "embedding_model": cfg.embedding_model,
        "embedding_dimensions": cfg.embedding_dimensions,
        "generation_model": cfg.generation_model,
        "long_term_memory": cfg.long_term_memory,
        "forgetting_enabled": cfg.forgetting_enabled,
        "health": health,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0 if health.get("ok") else 1
    print(
        f"alfred-brain ams: {provider_base_url} "
        f"embedding={cfg.embedding_model} generation={cfg.generation_model} "
        f"dim={cfg.embedding_dimensions}"
    )
    if provider_base_url != cfg.base_url:
        print(f"  configured server base: {cfg.base_url}", file=sys.stderr)
    if health.get("ok"):
        print(f"  health ok namespace={health.get('namespace')}")
        return 0
    print(f"  health unavailable: {health.get('error', 'unknown error')}", file=sys.stderr)
    return 1


def cmd_redis_sync(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    provider = _build_redis_provider()
    codename = None if args.codename in (None, "-") else args.codename
    repo = None if args.repo in (None, "-") else args.repo
    lessons = brain.recall(
        codename=codename,
        repo=repo,
        query=args.query,
        limit=args.limit,
    )
    synced = 0
    failed: list[str] = []
    for lesson in lessons:
        if args.dry_run:
            synced += 1
            continue
        if provider.sync_lesson(lesson):
            synced += 1
        else:
            failed.append(lesson.id)

    payload = {
        "dry_run": bool(args.dry_run),
        "matched": len(lessons),
        "synced": synced,
        "failed": failed,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    elif args.dry_run:
        print(f"alfred-brain redis: would sync {synced} reviewed lesson(s)")
    else:
        print(f"alfred-brain redis: synced {synced}/{len(lessons)} reviewed lesson(s)")
        if failed:
            print(f"  failed: {', '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


def cmd_forget(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    if args.before:
        days = _parse_duration_days(args.before)
        if days is None:
            print(f"alfred-brain: cannot parse --before {args.before!r}", file=sys.stderr)
            return 2
        deleted = brain.forget_before(days=days)
        print(f"alfred-brain: deleted {deleted} lesson(s) older than {days}d")
        return 0
    if not args.id:
        print("alfred-brain: forget needs an id, or --before <duration>", file=sys.stderr)
        return 2
    ok = brain.forget(args.id)
    if ok:
        print(f"alfred-brain: forgot {args.id}")
        return 0
    print(f"alfred-brain: no lesson with id {args.id}", file=sys.stderr)
    return 1


def cmd_export(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    payload = brain.export()
    text = json.dumps(payload, indent=2, default=str)
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        print(f"alfred-brain: exported {len(payload['lessons'])} lesson(s) to {out_path}")
        return 0
    print(text)
    return 0


def _parse_duration_days(value: str) -> int | None:
    """Accept ``30d``, ``30``, ``2w``, ``6h`` (rounded down). Returns days."""
    m = re.fullmatch(r"\s*(\d+)\s*([dwh]?)\s*", value.lower())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2) or "d"
    if unit == "d":
        return n
    if unit == "w":
        return n * 7
    if unit == "h":
        # Hour granularity rounded down to days; one-hour TTL doesn't
        # make sense for a memory layer.
        return max(0, n // 24)
    return None


def _harvest_failure_pattern_memories(
    brain: FleetBrain,
    *,
    window_days: int,
    min_count: int,
    limit: int,
    apply: bool,
) -> list[dict[str, Any]]:
    existing = _existing_memory_keys(brain)
    proposals: list[dict[str, Any]] = []
    for pattern in brain.list_failure_patterns(
        window_days=window_days,
        min_count=min_count,
        limit=limit,
    ):
        codename = str(pattern.get("codename") or "operator").strip() or "operator"
        repo = str(pattern.get("repo") or "global").strip() or "global"
        pattern_key = _harvest_pattern_key(pattern)
        body = _failure_pattern_memory_body(pattern)
        status = "preview"
        candidate_id: str | None = None
        if pattern_key in existing:
            status = "duplicate"
        elif apply:
            candidate_id = _harvest_candidate_id(pattern_key)
            try:
                candidate = brain.propose_memory(
                    codename=codename,
                    repo=repo,
                    body=body,
                    tags=[
                        "auto-harvest",
                        "failure-pattern",
                        f"class:{pattern.get('classification') or 'unknown'}",
                        f"pattern:{pattern_key}",
                    ],
                    severity=pattern.get("severity", "warning"),
                    source="memory-harvest",
                    evidence=json.dumps(
                        {
                            "kind": "failure_pattern",
                            "pattern_key": pattern_key,
                            "count": pattern.get("count"),
                            "first_seen": pattern.get("first_seen"),
                            "last_seen": pattern.get("last_seen"),
                            "latest_summary": pattern.get("latest_summary"),
                            "suggested_action": pattern.get("suggested_action"),
                        },
                        sort_keys=True,
                    ),
                    confidence=0.72,
                    candidate_id=candidate_id,
                )
            except Exception as exc:
                if not _looks_like_duplicate_candidate(exc):
                    raise
                status = "duplicate"
            else:
                candidate_id = str(getattr(candidate, "id", candidate))
                existing.add(pattern_key)
                status = "queued"
        proposals.append(
            {
                "status": status,
                "candidate_id": candidate_id,
                "codename": codename,
                "repo": repo,
                "body": body,
                "pattern": pattern,
            }
        )
    return proposals


def _existing_memory_keys(brain: FleetBrain) -> set[str]:
    keys: set[str] = set()
    for lesson in brain.list_lessons(limit=10_000):
        keys.update(_pattern_keys_from_tags(getattr(lesson, "tags", []) or []))
    for candidate in brain.list_memory_candidates(status=None, limit=10_000):
        if str(getattr(candidate, "status", "") or "").lower() in {"rejected", "retired"}:
            continue
        keys.update(_pattern_keys_from_tags(getattr(candidate, "tags", []) or []))
        key = _pattern_key_from_evidence(getattr(candidate, "evidence", "") or "")
        if key:
            keys.add(key)
    return keys


def _harvest_pattern_key(pattern: dict[str, Any]) -> str:
    raw = str(pattern.get("key") or "").strip()
    if raw:
        return raw
    parts = [
        str(pattern.get("codename") or "operator").strip() or "operator",
        str(pattern.get("repo") or "-").strip() or "-",
        str(pattern.get("subtype") or "unknown").strip() or "unknown",
        str(pattern.get("engine") or "-").strip() or "-",
    ]
    return "|".join(parts)


def _harvest_candidate_id(pattern_key: str) -> str:
    digest = hashlib.sha256(pattern_key.encode("utf-8")).hexdigest()[:20]
    return f"harvest-{digest}"


def _looks_like_duplicate_candidate(exc: Exception) -> bool:
    if not isinstance(exc, sqlite3.IntegrityError):
        return False
    text = str(exc).lower()
    return "memory_candidates" in text and "unique" in text


def _pattern_keys_from_tags(tags: object) -> set[str]:
    keys: set[str] = set()
    if not isinstance(tags, list):
        return keys
    for tag in tags:
        text = str(tag).strip()
        if text.startswith("pattern:") and text.removeprefix("pattern:").strip():
            keys.add(text.removeprefix("pattern:").strip())
    return keys


def _pattern_key_from_evidence(evidence: object) -> str | None:
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except json.JSONDecodeError:
            return None
    if isinstance(evidence, dict):
        key = evidence.get("pattern_key")
        return str(key).strip() if key else None
    return None


def _failure_pattern_memory_body(pattern: dict[str, Any]) -> str:
    codename = str(pattern.get("codename") or "operator").strip() or "operator"
    repo = str(pattern.get("repo") or "global").strip() or "global"
    subtype = str(pattern.get("subtype") or "unknown").strip() or "unknown"
    action = str(pattern.get("suggested_action") or "inspect_before_rerun").strip()
    classification = str(pattern.get("classification") or "unknown").strip()
    latest = str(pattern.get("latest_summary") or "").strip()
    count = pattern.get("count") or "multiple"
    body = (
        f"When {codename} repeatedly hits {subtype} on {repo}, treat it as "
        f"{classification.replace('_', ' ')} and {action.replace('_', ' ')} before rerunning. "
        f"Seen at least {count} times as of harvest time."
    )
    if latest:
        body = f"{body} Latest evidence: {_short(latest, 180)}"
    return body


def _short(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", value.strip())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def _candidate_to_dict(candidate) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {
        "id": candidate.id,
        "codename": candidate.codename,
        "repo": candidate.repo,
        "body": candidate.body,
        "tags": candidate.tags,
        "severity": candidate.severity,
        "source": candidate.source,
        "source_firing_id": candidate.source_firing_id,
        "evidence": candidate.evidence,
        "confidence": candidate.confidence,
        "status": candidate.status,
        "created_at": candidate.created_at.astimezone(UTC).isoformat(),
        "reviewed_at": candidate.reviewed_at.astimezone(UTC).isoformat()
        if candidate.reviewed_at
        else None,
        "reviewed_by": candidate.reviewed_by,
        "review_note": candidate.review_note,
        "promoted_lesson_id": candidate.promoted_lesson_id,
    }


def _failure_to_dict(event) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {
        "id": event.id,
        "codename": event.codename,
        "repo": event.repo,
        "firing_id": event.firing_id,
        "subtype": event.subtype,
        "summary": event.summary,
        "engine": event.engine,
        "severity": event.severity,
        "created_at": event.created_at.astimezone(UTC).isoformat(),
    }


def _github_item_to_dict(item) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {
        "id": item.id,
        "repo": item.repo,
        "number": item.number,
        "kind": item.kind,
        "state": item.state,
        "title": item.title,
        "url": item.url,
        "labels": item.labels,
        "updated_at": item.updated_at.astimezone(UTC).isoformat(),
        "last_seen_at": item.last_seen_at.astimezone(UTC).isoformat(),
        "closed_at": item.closed_at.astimezone(UTC).isoformat() if item.closed_at else None,
        "merged_at": item.merged_at.astimezone(UTC).isoformat() if item.merged_at else None,
        "head_ref": item.head_ref,
        "base_ref": item.base_ref,
        "bundle_slug": item.bundle_slug,
    }


def _bundle_item_to_dict(item) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {
        "id": item.id,
        "bundle_slug": item.bundle_slug,
        "repo": item.repo,
        "item_kind": item.item_kind,
        "number": item.number,
        "state": item.state,
        "title": item.title,
        "url": item.url,
        "labels": item.labels,
        "updated_at": item.updated_at.astimezone(UTC).isoformat(),
        "last_seen_at": item.last_seen_at.astimezone(UTC).isoformat(),
    }


def _worker_to_dict(worker) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {
        "id": worker.id,
        "codename": worker.codename,
        "firing_id": worker.firing_id,
        "status": worker.status,
        "started_at": worker.started_at.astimezone(UTC).isoformat(),
        "heartbeat_at": worker.heartbeat_at.astimezone(UTC).isoformat(),
        "repo": worker.repo,
        "pid": worker.pid,
        "detail": worker.detail,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alfred-brain",
        description="Operator CLI for the fleet-brain memory layer.",
    )
    p.add_argument(
        "--db",
        help="Path to the SQLite brain file. Defaults to "
        "$ALFRED_FLEET_BRAIN_DB or $ALFRED_HOME/fleet-brain.db.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="row counts and db path")
    p_status.set_defaults(func=cmd_status)

    p_lessons = sub.add_parser("lessons", help="recall lessons for a codename / repo")
    p_lessons.add_argument("codename", help="codename or '-' to widen")
    p_lessons.add_argument("repo", help="repo full_name or '-' to widen")
    p_lessons.add_argument("--query", help="literal substring filter on body")
    p_lessons.add_argument("--limit", type=int, default=20)
    p_lessons.add_argument("--json", action="store_true")
    p_lessons.set_defaults(func=cmd_lessons)

    p_reflect = sub.add_parser("reflect", help="file a lesson from the shell")
    p_reflect.add_argument("codename")
    p_reflect.add_argument("repo")
    p_reflect.add_argument("body")
    p_reflect.add_argument("--tag", action="append", help="tag (repeatable)")
    p_reflect.add_argument("--severity", choices=["info", "warning", "blocker"], default="info")
    p_reflect.add_argument("--firing-id", dest="firing_id")
    p_reflect.add_argument(
        "--candidate",
        action="store_true",
        help="stage as a reviewable candidate instead of a trusted lesson",
    )
    p_reflect.add_argument("--confidence", type=float, default=0.5)
    p_reflect.set_defaults(func=cmd_reflect)

    p_propose = sub.add_parser("propose", help="stage a reviewable memory candidate")
    p_propose.add_argument("codename")
    p_propose.add_argument("repo")
    p_propose.add_argument("body")
    p_propose.add_argument("--tag", action="append", help="tag (repeatable)")
    p_propose.add_argument("--severity", choices=["info", "warning", "blocker"], default="info")
    p_propose.add_argument("--source", default="manual")
    p_propose.add_argument("--firing-id", dest="firing_id")
    p_propose.add_argument("--evidence")
    p_propose.add_argument("--confidence", type=float, default=0.5)
    p_propose.add_argument("--json", action="store_true")
    p_propose.set_defaults(func=cmd_propose)

    p_candidates = sub.add_parser("candidates", help="list reviewable memory candidates")
    p_candidates.add_argument(
        "--status",
        choices=["candidate", "validated", "rejected", "retired", "all"],
        default="candidate",
    )
    p_candidates.add_argument("--repo")
    p_candidates.add_argument("--codename")
    p_candidates.add_argument("--limit", type=int, default=50)
    p_candidates.add_argument("--json", action="store_true")
    p_candidates.set_defaults(func=cmd_candidates)

    p_promote = sub.add_parser("promote", aliases=["approve"], help="promote a candidate")
    p_promote.add_argument("id")
    p_promote.add_argument("--reviewer", default="operator")
    p_promote.add_argument("--note")
    p_promote.add_argument("--json", action="store_true")
    p_promote.set_defaults(func=cmd_promote)

    p_reject = sub.add_parser("reject", help="reject a candidate")
    p_reject.add_argument("id")
    p_reject.add_argument("--reviewer", default="operator")
    p_reject.add_argument("--note")
    p_reject.add_argument("--json", action="store_true")
    p_reject.set_defaults(func=cmd_reject)

    p_firings = sub.add_parser("firings", help="list firing audit rows")
    p_firings.add_argument("--codename")
    p_firings.add_argument("--status", choices=["ok", "blocked", "partial", "silent"])
    p_firings.add_argument("--limit", type=int, default=20)
    p_firings.set_defaults(func=cmd_firings)

    p_files = sub.add_parser("files", help="list recent file touches")
    p_files.add_argument("repo", help="repo full_name or '-' to widen")
    p_files.add_argument("--codename")
    p_files.add_argument("--path", help="exact repo-relative path")
    p_files.add_argument("--limit", type=int, default=50)
    p_files.add_argument("--json", action="store_true")
    p_files.set_defaults(func=cmd_files)

    p_failures = sub.add_parser("failures", help="list normalized non-success events")
    p_failures.add_argument("--repo")
    p_failures.add_argument("--codename")
    p_failures.add_argument("--subtype")
    p_failures.add_argument("--limit", type=int, default=50)
    p_failures.add_argument("--json", action="store_true")
    p_failures.set_defaults(func=cmd_failures)

    p_failure_patterns = sub.add_parser(
        "failure-patterns",
        help="group repeated failures and suggest operator actions",
    )
    p_failure_patterns.add_argument("--repo")
    p_failure_patterns.add_argument("--codename")
    p_failure_patterns.add_argument("--window-days", type=int, default=7)
    p_failure_patterns.add_argument("--min-count", type=int, default=2)
    p_failure_patterns.add_argument("--limit", type=int, default=20)
    p_failure_patterns.add_argument("--json", action="store_true")
    p_failure_patterns.set_defaults(func=cmd_failure_patterns)

    p_governor = sub.add_parser(
        "governor",
        help="reliability governor report with failures, stale workers, and promotions",
    )
    p_governor.add_argument("--window-days", type=int, default=7)
    p_governor.add_argument("--min-count", type=int, default=2)
    p_governor.add_argument("--stale-worker-minutes", type=int, default=60)
    p_governor.add_argument("--limit", type=int, default=10)
    p_governor.add_argument("--json", action="store_true")
    p_governor.set_defaults(func=cmd_governor)

    p_github = sub.add_parser("github", help="list cached GitHub issue/PR state")
    p_github.add_argument("--repo")
    p_github.add_argument("--kind", choices=["issue", "pr"])
    p_github.add_argument("--state", choices=["open", "closed", "merged", "unknown"])
    p_github.add_argument("--bundle")
    p_github.add_argument("--limit", type=int, default=50)
    p_github.add_argument("--json", action="store_true")
    p_github.set_defaults(func=cmd_github)

    p_bundles = sub.add_parser("bundles", help="list cached bundle memberships")
    p_bundles.add_argument("bundle", nargs="?")
    p_bundles.add_argument("--state", choices=["open", "closed", "merged", "unknown"])
    p_bundles.add_argument("--limit", type=int, default=50)
    p_bundles.add_argument("--json", action="store_true")
    p_bundles.set_defaults(func=cmd_bundles)

    p_workers = sub.add_parser("workers", help="list worker heartbeats")
    p_workers.add_argument("--codename")
    p_workers.add_argument("--status", choices=["running", "ok", "failed", "stale", "cancelled"])
    p_workers.add_argument("--stale", action="store_true", help="show stale running workers only")
    p_workers.add_argument("--max-age-minutes", type=int, default=60)
    p_workers.add_argument("--limit", type=int, default=50)
    p_workers.add_argument("--json", action="store_true")
    p_workers.set_defaults(func=cmd_workers)

    p_heartbeat = sub.add_parser("heartbeat", help="record one worker heartbeat")
    p_heartbeat.add_argument("codename")
    p_heartbeat.add_argument("firing_id")
    p_heartbeat.add_argument(
        "--status", choices=["running", "ok", "failed", "stale", "cancelled"], default="running"
    )
    p_heartbeat.add_argument("--repo")
    p_heartbeat.add_argument("--pid", type=int)
    p_heartbeat.add_argument("--detail")
    p_heartbeat.add_argument("--json", action="store_true")
    p_heartbeat.set_defaults(func=cmd_heartbeat)

    p_promotions = sub.add_parser("promotions", help="suggest high-confidence memory promotions")
    p_promotions.add_argument("--min-confidence", type=float, default=0.75)
    p_promotions.add_argument("--limit", type=int, default=20)
    p_promotions.add_argument("--json", action="store_true")
    p_promotions.set_defaults(func=cmd_promotions)

    p_doctor = sub.add_parser("doctor", help="memory-layer health checks")
    p_doctor.add_argument("--json", action="store_true")
    p_doctor.set_defaults(func=cmd_doctor)

    p_harvest = sub.add_parser(
        "harvest",
        help="propose reviewable memories from repeated failure patterns",
    )
    p_harvest.add_argument("--window-days", type=int, default=7)
    p_harvest.add_argument("--min-count", type=int, default=2)
    p_harvest.add_argument("--limit", type=int, default=20)
    p_harvest.add_argument("--apply", action="store_true")
    p_harvest.add_argument("--json", action="store_true")
    p_harvest.set_defaults(func=cmd_harvest)

    p_auto_promote = sub.add_parser(
        "auto-promote",
        help="promote high-confidence, judge-approved candidates (ALFRED_AUTO_PROMOTE)",
    )
    p_auto_promote.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Confidence bar (default ALFRED_AUTO_PROMOTE_THRESHOLD or 0.9).",
    )
    p_auto_promote.add_argument(
        "--max-per-run",
        dest="max_per_run",
        type=int,
        default=None,
        help="Promotions per run (default ALFRED_AUTO_PROMOTE_MAX_PER_RUN or 5).",
    )
    p_auto_promote.add_argument("--json", action="store_true")
    p_auto_promote.set_defaults(func=cmd_auto_promote)

    p_redis_status = sub.add_parser("redis-status", help="check Redis Agent Memory Server")
    p_redis_status.add_argument("--json", action="store_true")
    p_redis_status.set_defaults(func=cmd_redis_status)

    p_ams_status = sub.add_parser("ams-status", help="show local Agent Memory Server status")
    p_ams_status.add_argument("--json", action="store_true")
    p_ams_status.set_defaults(func=cmd_ams_status)

    p_redis_sync = sub.add_parser("redis-sync", help="sync reviewed lessons to Redis AMS")
    p_redis_sync.add_argument("--codename", help="codename filter, or '-' to widen")
    p_redis_sync.add_argument("--repo", help="repo full_name filter, or '-' to widen")
    p_redis_sync.add_argument("--query", help="literal substring filter on body")
    p_redis_sync.add_argument("--limit", type=int, default=100)
    p_redis_sync.add_argument("--dry-run", action="store_true")
    p_redis_sync.add_argument("--json", action="store_true")
    p_redis_sync.set_defaults(func=cmd_redis_sync)

    p_forget = sub.add_parser("forget", help="delete a lesson or GC old ones")
    p_forget.add_argument("id", nargs="?", help="lesson id to delete")
    p_forget.add_argument("--before", help="GC older than e.g. '30d', '2w'")
    p_forget.set_defaults(func=cmd_forget)

    p_export = sub.add_parser("export", help="JSON snapshot of the brain")
    p_export.add_argument("--out", help="write to PATH instead of stdout")
    p_export.set_defaults(func=cmd_export)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("ALFRED_BRAIN_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
