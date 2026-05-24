# Operator analytics CLIs

`alfred metrics` and `alfred logs` are read-only tools for inspecting what
what the fleet wrote to disk. They never mutate state; they read
`$ALFRED_STATE_DIR` (default `$ALFRED_HOME/state`, default `~/.alfred/state`)
and print the result.

Both commands are thin argparse wrappers around library functions in
`lib/metrics.py` and `lib/transcripts.py`. Tests inject a fake `--state-dir`
to verify behaviour without touching the live tree.

## Configuration

| Env var | Purpose | Default |
| --- | --- | --- |
| `ALFRED_STATE_DIR` | Explicit state-root override | unset |
| `ALFRED_HOME` | Runtime root containing `state/` | `~/.alfred` |

Both commands also accept `--state-dir PATH` to override at the call site.

## `alfred metrics`

Roll up per-agent firings, cost, turns, tool-use, and Codex tokens across a
window. Reads:

- `<codename>/spend-YYYY-MM-DD.json` — per-day SpendState files
- `transcripts/<codename>/<YYYY-MM>/*.jsonl` — stream-JSON firings
- `codex/<codename>/<YYYY-MM>/*.stdout.txt` — Codex run stdout dumps

```sh
alfred metrics                          # last 7 days, per-agent
alfred metrics --since 14d              # last 14 days
alfred metrics --since 48h              # last 48h (rounds up to days)
alfred metrics --codename lucius        # one codename only
alfred metrics --by-day                 # daily totals instead of per-agent
alfred metrics --json                   # machine-readable
```

`--since` accepts `7`, `7d`, `48h`, `2w`, `1m`. `--days N` overrides
`--since` when both are passed.

### Example output

```
alfred-metrics — last 7 days @ 2026-05-23 12:17 UTC

codename     firings  ok    fail  turns   codex  ctok   cost     tools  top tool       skills
--------------------------------------------------------------------------------------------------
lucius       12       9     3     78      2      4500   $1.42    34     Bashx14        review x3
drake        8        7     1     52      0      0      $0.96    21     Readx9         -
your-agent   3        2     1     14      0      0      $0.42    2      Readx1         -
--------------------------------------------------------------------------------------------------
TOTAL        23       18    5     144     2      4500   $2.80    57

fleet success rate: 78.3%  (over 23 completed firings; 0 no-ops not counted)
```

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | User error (bad `--since`, unknown codename) |
| 2 | System error (state directory missing) |

## `alfred logs`

Inspect stream-JSON transcripts for one codename. Three modes:

```sh
alfred logs <codename>                                  # summary of last 10 firings
alfred logs <codename> --last 25                        # last 25 firings
alfred logs <codename> --show-tool-calls                # tool-call rollup
alfred logs <codename> --firing-id <id>                 # dump one firing
alfred logs <codename> --firing-id <id> --show-tool-calls
                                                        # tool calls for one firing
alfred logs <codename> --json                           # machine-readable
```

### Summary mode

```
alfred-logs lucius — last 3 firings
transcripts: ~/.alfred/state/transcripts/lucius/

firing_id              when                   subtype        turns  cost    tools  edits  top tools
---------------------------------------------------------------------------------------------------
2026-05-23-1417-abc    2026-05-23 14:17:00Z   success        4      $0.14   12     1      Bashx5, Readx4, Editx2 [skills: review]
2026-05-23-1305-def    2026-05-23 13:05:12Z   success        3      $0.09   8      0      Readx5, Bashx2
2026-05-22-2330-ghi    2026-05-22 23:30:01Z   error_max_turns 20     $0.43   45     0      Bashx18, Readx14
```

### Tool-call rollup

```
alfred-logs lucius --show-tool-calls — last 10 firings

tool          calls
------------------------
  Bash             47
  Read             32
  Edit             12
  Skill             3

skill invocations:
  /review                   2
  /qa                       1
```

### Firing dump

`--firing-id ID` pretty-prints a single transcript: system/user/assistant
events one per line, with tool-use blocks summarised inline.

```
[system] init
[assistant] Reading the package layout to understand what changed
[tool_use Read] /repo/your-backend/build.gradle.kts
[tool_use Bash] $ git log --oneline -10
[tool_use Skill] /review
[result] subtype=success turns=4 cost=$0.1400 stop_reason=end_turn
```

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | User error (unknown codename, missing firing id) |
| 2 | System error (state directory missing) |

## State directory layout

Both commands assume:

```
$ALFRED_HOME/state/
  <codename>/
    spend-YYYY-MM-DD.json
  transcripts/
    <codename>/
      <YYYY-MM>/
        <firing-id>.jsonl
  codex/
    <codename>/
      <YYYY-MM>/
        <firing-id>.stdout.txt
        <firing-id>.stderr.txt
        <firing-id>.last.md
```

The `claude_invoke_streaming()` helper in `lib/agent_runner.py` writes
transcripts here automatically. The SpendState helper writes spend files
on each firing. Hosts that have not run any agent yet will have an empty
or missing state directory; both CLIs degrade gracefully (exit 2 with a
clear message when `--state-dir` is absent, or render `(no firings or
transcripts in window)` when present but empty).

## Library reuse

The aggregation logic is importable:

```python
from pathlib import Path
from metrics import fleet_metrics
from transcripts import default_state_dir, list_firings, transcript_summary

state = default_state_dir()
report = fleet_metrics(state, days=7)
for m in report.metrics:
    if m.tool_calls_total > 50:
        print(m.codename, m.tool_calls)

for firing in list_firings(state, "lucius")[:5]:
    s = transcript_summary(firing.path)
    print(firing.firing_id, s.tool_calls_by_name)
```

The CLIs intentionally render only what dataclasses expose — extend the
dataclasses (or add new render formats) instead of growing the CLI code.
