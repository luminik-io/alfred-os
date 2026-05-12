#!/usr/bin/env python3
"""Robin - bug triage agent. Labels open issues by severity, asks for repro info, hands off to Lucius."""

from __future__ import annotations

import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")) + "/lib")
from agent_runner import (
    GH_ORG,
    STATE_ROOT,
    WORKSPACE_ROOT,
    EventLog,
    PreflightFailed,
    PreflightSpec,
    SpendState,
    agent_engine,
    claude_invoke_streaming,
    codex_invoke,
    codex_sandbox_for_agent,
    doctor_mode,
    engine_preflight_bins,
    ensure_labels,
    gh_issue_comment,
    gh_issue_edit,
    gh_json,
    invoke_agent_engine,
    is_globally_blocked,
    is_repo_paused,
    maybe_set_global_block_for_result,
    optional_env_int,
    preflight,
    short,
    slack_post,
    with_lock,
)

AGENT = os.environ.get("AGENT_CODENAME", "robin")
ROBIN_ENGINE = agent_engine(AGENT, default="hybrid")
LAUNCHD_LABEL = os.environ.get("LAUNCHD_LABEL", f"my.fleet.{AGENT}")

PREFLIGHT = PreflightSpec(
    agent=AGENT,
    bins=[*engine_preflight_bins(ROBIN_ENGINE), "gh", "git"],
    require_gh_auth=True,
)

TRIAGE_REPOS = [r.strip() for r in os.environ.get("ALFRED_ROBIN_REPOS", "").split(",") if r.strip()]

# Persistent dedup ledger. The same issue can survive a label-add failure
# (gh returns success but the label is missing on next read — eventual
# consistency or a rate-limit retry that didn't replay), so the on-disk
# ledger is the local-truth backstop. If we've touched the issue in the
# last TOUCHED_TTL_DAYS days, skip regardless of GitHub state.
TOUCHED_LEDGER = STATE_ROOT / AGENT / "touched.jsonl"
TOUCHED_TTL_DAYS = int(os.environ.get("ALFRED_ROBIN_TOUCHED_TTL_DAYS", "7"))
DAILY_TRIAGE_CAP = int(os.environ.get("ALFRED_ROBIN_DAILY_CAP", "50"))
DAILY_TURN_CAP = int(os.environ.get("ALFRED_ROBIN_TURN_CAP", "600"))

SEVERITY_LABELS = [
    ("severity:p0", "b60205", "Production broken / data loss / security leak"),
    ("severity:p1", "d93f0b", "User-visible bug, not blocking"),
    ("severity:p2", "fbca04", "Minor / polish"),
    ("severity:p3", "0e8a16", "Trivial / won't fix"),
    ("needs:info", "d4c5f9", "Reporter needs to provide more detail"),
    ("duplicate", "cccccc", "Duplicate of another issue"),
    ("bug", "ee0701", "Confirmed bug"),
    ("needs:triage", "fef2c0", "Needs Robin or human triage"),
]


def _load_touched() -> dict[str, str]:
    """Read the touched ledger -> {f"{repo}#{num}": iso_timestamp}.

    Old entries past TOUCHED_TTL_DAYS get pruned on read so the ledger
    self-trims and an issue Robin failed to label correctly becomes
    eligible again after a week."""
    if not TOUCHED_LEDGER.exists():
        return {}
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=TOUCHED_TTL_DAYS)
    out: dict[str, str] = {}
    for line in TOUCHED_LEDGER.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            ts = datetime.datetime.fromisoformat(entry["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.UTC)
            if ts < cutoff:
                continue
            out[entry["key"]] = entry["ts"]
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return out


def _record_touched(repo: str, num: int) -> None:
    """Append a touched-issue marker. Atomic enough for the firing cadence."""
    TOUCHED_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "key": f"{repo}#{num}",
        "ts": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    with open(TOUCHED_LEDGER, "a") as f:
        f.write(json.dumps(entry) + "\n")


def list_untriaged() -> list[tuple[str, dict]]:
    """Across in-scope repos, find open issues with no severity label and no agent:implement,
    and that Robin has not touched in the last TOUCHED_TTL_DAYS days."""
    touched = _load_touched()
    candidates = []
    for repo in TRIAGE_REPOS:
        if is_repo_paused(repo):
            continue
        issues = gh_json(
            [
                "gh",
                "issue",
                "list",
                "-R",
                f"{GH_ORG}/{repo}",
                "--state",
                "open",
                "--json",
                "number,title,body,labels,createdAt,author",
                "--limit",
                "30",
            ],
            default=[],
        )
        for i in issues:
            key = f"{repo}#{i['number']}"
            if key in touched:
                continue
            label_names = [lbl["name"] for lbl in i.get("labels", [])]
            if any(n.startswith("severity:") for n in label_names):
                continue
            if "agent:implement" in label_names:
                continue
            if "needs:human-scope" in label_names:
                continue
            # Issue already in the lifecycle - skip; implementer / automerge own it
            if "agent:in-flight" in label_names or "agent:pr-open" in label_names:
                continue
            if "do-not-pickup" in label_names:
                continue
            if "done-already" in label_names:
                continue
            candidates.append((repo, i))
    # Newest first
    candidates.sort(key=lambda x: x[1]["createdAt"], reverse=True)
    return candidates[:5]  # max 5 per firing


def candidate_keys(candidates: list[tuple[str, dict]]) -> set[tuple[str, int]]:
    """Return the exact repo/issue pairs Robin is allowed to mutate."""
    keys: set[tuple[str, int]] = set()
    for repo, issue in candidates:
        try:
            num = int(issue["number"])
        except (KeyError, TypeError, ValueError):
            continue
        keys.add((repo, num))
    return keys


def validated_triages(
    triages: object,
    candidates: list[tuple[str, dict]],
) -> tuple[list[dict], list[str]]:
    """Filter LLM output to the candidate allowlist before GitHub writes."""
    if not isinstance(triages, list):
        return [], ["triages is not a list"]

    allowed = candidate_keys(candidates)
    valid: list[dict] = []
    rejected: list[str] = []
    seen: set[tuple[str, int]] = set()
    for idx, item in enumerate(triages):
        if not isinstance(item, dict):
            rejected.append(f"triage[{idx}] is not an object")
            continue
        repo = item.get("repo")
        num = item.get("number")
        if not isinstance(repo, str) or not isinstance(num, int) or isinstance(num, bool):
            rejected.append(f"triage[{idx}] has invalid repo/number")
            continue
        key = (repo, num)
        if key not in allowed:
            rejected.append(f"triage[{idx}] targets non-candidate {repo}#{num}")
            continue
        if key in seen:
            rejected.append(f"triage[{idx}] duplicates {repo}#{num}")
            continue
        seen.add(key)
        valid.append(item)
    return valid, rejected


def main() -> int:
    with_lock(AGENT)

    try:
        preflight(PREFLIGHT)
    except PreflightFailed:
        return 0

    if doctor_mode():
        print(f"[{AGENT.upper()}-DOCTOR-OK]")
        return 0

    if not TRIAGE_REPOS:
        print(f"[{AGENT.upper()}-IDLE] no repos configured (set ALFRED_ROBIN_REPOS)")
        return 0

    events = EventLog(agent=AGENT)
    events.emit("firing_started")

    blocked = is_globally_blocked()
    if blocked:
        print(f"[{AGENT.upper()}-GLOBAL-BLOCKED] {blocked}. Skipping firing.")
        events.emit("firing_complete", outcome="global-blocked")
        return 0
    spend = SpendState(AGENT)

    if spend.state.get("triaged_today", 0) >= DAILY_TRIAGE_CAP:
        print(
            f"[{AGENT.upper()}-DAILY-CAP] triaged_today={spend.state.get('triaged_today', 0)} >= {DAILY_TRIAGE_CAP}. Skipping."
        )
        events.emit("firing_complete", outcome="triage-cap")
        return 0
    if spend.state["turns_today"] >= DAILY_TURN_CAP:
        msg = f"[{AGENT.upper()}-DAILY-CAP] turns={spend.state['turns_today']} >= {DAILY_TURN_CAP}. Skipping."
        print(msg)
        slack_post(msg)
        events.emit("firing_complete", outcome="turn-cap")
        return 0

    candidates = list_untriaged()
    events.emit("issues_inspected", count=len(candidates))
    if not candidates:
        print(f"[{AGENT.upper()}-IDLE]")
        events.emit("firing_complete", outcome="idle-no-candidates")
        return 0

    # Pre-ensure labels exist on every repo we might touch
    for repo in {c[0] for c in candidates}:
        ensure_labels(repo, SEVERITY_LABELS)

    # Build one LLM prompt that triages all candidates in a single call
    items_block = "\n\n".join(
        f"### {GH_ORG}/{repo}#{i['number']}\n"
        f"Title: {i['title']}\n"
        f"Author: {i.get('author', {}).get('login', '?')}\n"
        f"Created: {i['createdAt']}\n"
        f"Body:\n{(i.get('body') or '')[:1500]}"
        for repo, i in candidates
    )

    prompt = f"""You are {AGENT.title()}, the bug-triage agent. Classify each issue below by severity and decide the next-step label.

You do not write code. You label and comment.

Severity rules:
- severity:p0 = production broken, data loss, security leak. Use sparingly. Real production systems only.
- severity:p1 = user-visible bug, not blocking. Reproducible.
- severity:p2 = minor polish, low-impact UX issue, dev-only annoyance.
- severity:p3 = trivial or won't-fix.

Action rules:
- If issue has clear repro steps + scoped path forward: severity + agent:implement (the implementer will pick up).
- If issue is vague (no repro, no specific file/screen): severity + needs:info + a comment with 2-3 specific clarifying questions.
- If duplicate of a known issue (mention #N in your reasoning): severity + duplicate.
- Documentation-drift issues are NEVER security P0. Use severity:p1 or p2.

Voice rules:
- Comments should be < 100 words.
- No em-dashes anywhere. No "unlock", "leverage", "seamless", "transform". No fabricated numbers.
- If you mention a clarifying question, be specific: ask for browser+OS, exact URL, expected vs actual.

Issues to triage:

{items_block}

Output - print EXACTLY this JSON to stdout, nothing else:

{{
  "triages": [
    {{
      "repo": "<repo-slug>",
      "number": 123,
      "severity": "severity:p1",
      "extra_labels": ["agent:implement"],
      "comment": "Optional - leave empty string if no comment needed"
    }},
    ...
  ]
}}
"""

    def _on_engine_fallback(fallback_result):
        events.emit(
            "llm_fallback",
            from_engine="claude",
            to_engine="codex",
            reason=fallback_result.error_message or fallback_result.result_text,
        )

    result, engine_used = invoke_agent_engine(
        prompt,
        engine=ROBIN_ENGINE,
        claude_fn=claude_invoke_streaming,
        codex_fn=codex_invoke,
        workdir=WORKSPACE_ROOT,
        claude_allowed_tools="Read,Bash,Glob,Grep",
        agent=AGENT,
        firing_id=events.firing_id,
        claude_max_turns=optional_env_int("ALFRED_ROBIN_MAX_TURNS", minimum=20),
        timeout=600,
        codex_timeout=600,
        codex_sandbox=codex_sandbox_for_agent(AGENT, default="workspace-write"),
        codex_bypass_approvals_and_sandbox=True,
        codex_add_dirs=[WORKSPACE_ROOT],
        on_fallback=_on_engine_fallback,
    )
    spend.increment(firings_today=1, turns_today=result.num_turns, cost_usd_today=result.cost_usd)
    events.emit(
        "llm_invoke_done",
        engine=engine_used,
        turns=result.num_turns,
        subtype=result.subtype,
        success=result.success,
    )

    if not result.success:
        spend.increment(failures_today=1, consecutive_failures=1)
        until = maybe_set_global_block_for_result(AGENT, result, engine_used=engine_used)
        if until:
            msg = (
                f"{AGENT.title()} hit provider rate limit ({result.subtype}, engine={engine_used}). "
                f"Global block until {until}."
            )
            print(msg)
            slack_post(msg, severity="alert")
            events.emit("firing_complete", outcome=f"llm-{result.subtype}", engine=engine_used)
            return 0
        msg = f"❌ {AGENT.title()}: engine={engine_used} subtype={result.subtype} turns={result.num_turns}"
        print(msg)
        slack_post(msg)
        events.emit("firing_complete", outcome=f"llm-{result.subtype}", engine=engine_used)
        return 0

    # Parse the LLM JSON response
    text = (result.result_text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text).rstrip("`").rstrip()
    if text.endswith("```"):
        text = text[:-3].rstrip()

    try:
        parsed = json.loads(text)
        raw_triages = parsed.get("triages", [])
    except (json.JSONDecodeError, AttributeError) as e:
        msg = f"❌ {AGENT.title()}: could not parse {engine_used} JSON output ({e}). First line: {short(text.splitlines()[0] if text else '', 100)}"
        print(msg)
        slack_post(msg)
        events.emit("firing_complete", outcome="parse-error")
        return 0

    triages, rejected_triages = validated_triages(raw_triages, candidates)
    if rejected_triages:
        events.emit("triages_rejected", count=len(rejected_triages), reasons=rejected_triages[:5])

    # Apply each triage decision
    summary_lines = []
    for t in triages:
        repo = t.get("repo")
        num = t.get("number")
        severity = t.get("severity")
        extra = t.get("extra_labels", []) or []
        comment = (t.get("comment") or "").strip()

        if not repo or not num or not severity:
            continue
        if not severity.startswith("severity:"):
            continue

        labels_to_add = [severity] + [
            lbl
            for lbl in extra
            if lbl
            in {
                "agent:implement",
                "needs:info",
                "duplicate",
                "bug",
                "needs:triage",
            }
        ]
        ok = gh_issue_edit(repo, num, add_labels=labels_to_add)
        if comment:
            gh_issue_comment(repo, num, comment)
        # Record the touch even on failure - the next firing will re-pull
        # the same issue otherwise. The TTL window allows a re-try after 7 days.
        _record_touched(repo, num)
        events.emit(
            "triaged",
            repo=f"{GH_ORG}/{repo}",
            number=num,
            severity_label=severity,
            extra_labels=extra,
        )
        flag = "" if ok else " (label apply RC!=0)"
        summary_lines.append(
            f"- {repo}#{num} → {severity}{flag}" + (f" + {','.join(extra)}" if extra else "")
        )

    spend.increment(triaged_today=len(triages))

    # Reset consecutive_failures and count this firing as a success even when
    # no triages applied — the firing completed without error.
    spend.increment(successes_today=1)
    spend.set(consecutive_failures=0)

    if not summary_lines:
        print(f"[{AGENT.upper()}-NO-OP] no triages applied (parse OK but empty list)")
        events.emit("firing_complete", outcome="no-triages-applied")
        return 0

    msg = (
        f"🐦 {AGENT.title()}: triaged {len(summary_lines)} issue(s) (engine={engine_used}, turns={result.num_turns})\n"
        + "\n".join(summary_lines)
    )
    print(msg)
    slack_post(msg)
    events.emit("firing_complete", outcome="triaged", count=len(summary_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
