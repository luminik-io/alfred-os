# alfred-os — docs index

Top-level docs (one level up):
- [`README.md`](../README.md) — what alfred-os is, quick start, codename pattern, status
- [`ARCHITECTURE.md`](../ARCHITECTURE.md) — design rationale: why cron, why worktrees, why per-agent IAM
- [`BOOTSTRAP.md`](../BOOTSTRAP.md) — fresh-fork setup walkthrough
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — PR criteria, codename proposals, prompt-change flow

Concept docs:
- [`STATE_MACHINE.md`](STATE_MACHINE.md) — issue claim lifecycle, race resolution, stale-claim sweep, operator overrides

Code:
- [`lib/agent_runner.py`](../lib/agent_runner.py) — the shared library
- [`bin/`](../bin/) — `hermes-claude` switcher, `doctor.sh` host validator
- [`launchd/`](../launchd/) — plist template + render.sh + agents.conf format
- [`examples/`](../examples/) — reference codename agents, label-state CLI, pre-push hook

Tests:
- [`tests/test_agent_runner.py`](../tests/test_agent_runner.py) — 35 cases covering preflight, doctor_mode, load_prompt, commit_trailer, HandoffTable, EventLog, _full_repo, slack severity routing, repo pause/resume, claim-comment parsing, issue_dedup_check
- Run: `uv run --with pytest pytest tests/` (or `python3 -m pytest tests/` if pytest is on PATH)
