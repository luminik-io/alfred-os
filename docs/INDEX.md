# pennyworth — docs index

Top-level docs (one level up):
- [`README.md`](../README.md) — what pennyworth is, quick start, codename pattern, status
- [`ARCHITECTURE.md`](../ARCHITECTURE.md) — design rationale: why cron, why worktrees, why per-agent IAM
- [`BOOTSTRAP.md`](../BOOTSTRAP.md) — fresh-fork setup walkthrough
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — PR criteria, codename proposals, prompt-change flow

Code:
- [`lib/agent_runner.py`](../lib/agent_runner.py) — the shared library
- [`bin/`](../bin/) — `hermes-claude` switcher, `doctor.sh` host validator
- [`launchd/`](../launchd/) — plist template + render.sh + agents.conf format
- [`examples/`](../examples/) — reference codename agents to copy

Tests:
- [`tests/test_agent_runner.py`](../tests/test_agent_runner.py) — 22 cases covering preflight, doctor_mode, load_prompt, commit_trailer, HandoffTable, EventLog, _full_repo
- Run: `uv run --with pytest pytest tests/` (or `python3 -m pytest tests/` if pytest is on PATH)
