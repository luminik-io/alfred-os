# Slop detector

> AI-slop scanner for any directory of prose. Catches the LLM-cliche
> vocabulary, phrases, and rhythms most often produced by an LLM-authored
> first draft. Read-only, stdlib-only, exits non-zero in CI.

Alfred ships a fleet of autonomous agents. Most of them ship marketing
copy that an LLM helped draft. The slop detector is the quality gate
that catches "seamlessly transform your workflow" before it lands on the
site. No peer agent runtime scans its own output for the vocabulary
that gives LLM-authored text away. Alfred does.

## What it does

`alfred slop-detect` walks a directory, reads every file matching the
rule pack's include globs (`*.md`, `*.mdx`, `*.astro`, `*.html`, etc.),
and reports every match against the configured rule pack. Code fences,
inline backticks, HTML comments, and JSX comments are stripped before
matching, so an intentional example like "we never say 'leverage'" does
not flag itself.

Three severities are conventional:

- **DRIFT** (most severe): canon-violating vocabulary or claims. Use for
  category-defining terms you cannot ship under any draft.
- **CAUTION**: LLM-cliche voice. The default rule pack lives here.
- **TYPO** (least severe): formatting and hyphenation lints.

Severity order comes from the rule pack itself (the `severities` array,
most severe first). The `--min-severity` CLI flag uses that order to
gate which findings can fail the run.

## Why we ship it

Buyers learn to spot LLM-authored copy fast. "Unlock", "leverage",
"seamless", "transform", "your stack", "the part I did not see coming",
and the "X. Y. The Z." three-beat rhythm are tells. Once a prospect
clocks the copy as AI-drafted, the trust budget for the rest of the page
drops. This tool runs in CI on every PR that touches public copy and
exits 1 if it finds slop. The opinionated default pack is enough on day
one; teams customize as their voice matures.

## CLI

```sh
alfred slop-detect [--path <dir>] [--rules <json>] \
                   [--report md|json] [--fail-on-match] \
                   [--min-severity <name>] [--max-findings <n>]
```

| Flag | Default | Purpose |
|---|---|---|
| `--path` | `$ALFRED_SLOP_TARGET_PATH` or `.` | Directory to scan. |
| `--rules` | `$ALFRED_SLOP_RULES` or bundled pack | JSON rule pack path. |
| `--report` | `md` | Output format: `md` or `json`. |
| `--fail-on-match` | off | Exit 1 if any qualifying finding is reported. |
| `--min-severity` | unset | Only count findings at this severity or higher. |
| `--max-findings` | unset | Markdown only: cap itemized findings. |
| `--quiet` | off | Suppress info logs; findings still print. |

Exit codes:

- `0` clean (or no qualifying findings under `--min-severity`)
- `1` findings present and `--fail-on-match` was supplied
- `2` system error (bad path, bad rule pack, etc.)

## Rule pack format

A rule pack is a JSON object with metadata and a list of rules:

```json
{
  "name": "alfred-default",
  "version": "1.0.0",
  "description": "Opinionated default AI-slop rule pack.",
  "severities": ["DRIFT", "CAUTION", "TYPO"],
  "skip_dirs": [".git", "node_modules", "dist", ".cache"],
  "include_globs": ["*.md", "*.mdx", "*.astro", "*.html"],
  "rules": [
    {
      "id": "word.seamless",
      "type": "word",
      "severity": "CAUTION",
      "value": "seamless",
      "reason": "LLM-cliche adjective."
    }
  ]
}
```

Rule fields:

- `id` (string, required): stable identifier, used in reports.
- `type` (string, required): one of `word`, `phrase`, `regex`, `pattern`.
- `severity` (string, required): must appear in the pack's `severities` list.
- `value` (string, required): the word, phrase, or regex source.
- `reason` (string, optional): displayed in the report alongside the match.

Rule types:

- **`word`**: matches the value as a whole word (`\bvalue\b`,
  case-insensitive). Best for single tokens. Inflections are not
  expanded automatically (list `transform` and `transforms` as separate
  rules if you want both).
- **`phrase`**: matches the value with flexible internal whitespace,
  bounded by word breaks. `"your stack"` will catch `"your  stack"` but
  not `"your-stack"`.
- **`regex`** / **`pattern`**: compiled directly with `re.IGNORECASE`.
  Use `pattern` to label rhythmic / structural rules separately for
  readability; both behave identically.

## Default rule pack

The bundled pack at `examples/slop-rules.json` is **opinionated**. It
catches the vocabulary, phrases, and patterns that mark text as
LLM-authored:

**Banned words (severity CAUTION)**: `seamless`, `seamlessly`, `unlock`,
`leverage`, `transform`, `synergy`, `cutting-edge`, `revolutionize`,
`streamline`, `empower`, `delve`.

**Banned phrases (severity CAUTION)**: `dive in`, `in the loop`,
`your stack`, `your journey`, `game changer`, `world class`,
`best in class`, `it is not just`.

**Banned phrases (severity DRIFT)**: `the part I did not see coming`
(a particularly strong LinkedIn-LLM hook tell).

**Banned patterns (severity CAUTION)**:

- The `X. Y. The Z.` three-beat rhythm at the end of a paragraph
  (`(?m)^[A-Z][^.\n]{2,40}\. [A-Z][^.\n]{2,40}\. The [A-Z][^.\n]{2,40}\.\s*$`).
- Unicode dash characters. Use ` - ` (hyphen between spaces) or restructure.

Override any of this by passing your own pack via `--rules` or by
setting `$ALFRED_SLOP_RULES`. Start from the bundled file, edit, point
at it.

## Before and after

A first draft from an LLM:

> This solution seamlessly transforms your workflow, helping you unlock
> the full power of your stack. The part I did not see coming: how
> quickly the team dove in.

The detector emits four CAUTION findings (`seamlessly`, `transform`,
`your stack`, `dive in`) and one DRIFT finding
(`the part I did not see coming`). After a rewrite:

> The integration writes attendee data into your CRM during the event
> and reconciles it against your meeting list at the close. The team
> spent one day on setup.

Clean.

## CI integration

GitHub Actions:

```yaml
# .github/workflows/slop-check.yml
name: slop-check
on:
  pull_request:
    paths:
      - "site/**"
      - "docs/**"
      - "**.md"
      - "**.mdx"
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Scan for AI-slop
        run: |
          python3 bin/slop-detector.py \
            --path site/src \
            --rules examples/slop-rules.json \
            --fail-on-match \
            --min-severity CAUTION
```

Pre-commit hook (`.pre-commit-config.yaml` snippet):

```yaml
- repo: local
  hooks:
    - id: slop-detect
      name: AI-slop scan
      entry: python3 bin/slop-detector.py --path . --fail-on-match
      language: system
      pass_filenames: false
      stages: [pre-commit]
```

## Customization recipes

**Add a banned word to the default pack** without forking the file:
copy `examples/slop-rules.json` into your repo as `slop-rules.json`,
add your rule, and pass `--rules ./slop-rules.json`. The detector reads
the file you point at, nothing else.

**Allow an LLM-cliche in a specific document**: wrap the offending
passage in an inline backtick span or a fenced code block; the detector
strips those regions before matching. (This is how you keep canonical
vocab lists in docs without the detector flagging itself.)

**Reduce noise during a migration**: pass `--min-severity DRIFT` so only
your most-severe rules fail CI. Promote rules to DRIFT as the codebase
catches up.

**Run on a schedule with Slack output**: enable the optional `curator`
agent (see [`docs/AGENTS.md`](AGENTS.md)). It fires the detector weekly,
formats a Slack message, and never modifies any file.

## Library entry points

For embedders who want to call the scanner from Python:

```python
from pathlib import Path
from slop_detector import load_rule_pack, scan_path, render_markdown

pack = load_rule_pack(Path("examples/slop-rules.json"))
report = scan_path(Path("site/src"), pack)
print(render_markdown(report))
print(f"total findings: {report.total_findings}")
```

`scan_path` accepts any `RulePack` dataclass, so tests inject in-memory
packs via `rule_pack_from_dict(...)` without writing JSON to disk.
