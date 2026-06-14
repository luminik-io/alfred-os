# `alfred serve`

A small, localhost-only dashboard over `$ALFRED_HOME/state`, saved Batman
plans, the local fleet brain, and local planning drafts. It is read-only for
runtime state and can write issue/spec drafts under `$ALFRED_HOME/planning-drafts`.
The operator's pane of glass for "what is the fleet doing right now".

Status: v0.4.0 shipped the first dashboard. v0.4.1 adds reliability-governor
cards, human-readable timestamps, responsive table shells, mobile card
layouts, a sticky header, a saved-plan inbox, a Planning intake page, external
issue/PR links that open in a separate tab, and action summaries as a
cross-platform precursor to the Tauri client under `clients/desktop`.

## Install

The server lives behind an optional dependency group so the base `alfred-os` package stays stdlib-only:

```bash
pip install 'alfred-os[serve]'
```

The group pulls in `fastapi`, `uvicorn`, and `jinja2`. Pico.css and HTMX are loaded from a CDN by the bundled templates; no JS or CSS build step is required.

## Run

From a checkout:

```bash
python bin/alfred-serve.py
# or
python bin/alfred serve
```

From a deployed checkout:

```bash
alfred serve
```

Defaults:

| flag           | default       | meaning                                                       |
| -------------- | ------------- | ------------------------------------------------------------- |
| `--host`       | `127.0.0.1`   | bind address. Use `0.0.0.0` only on a trusted LAN.            |
| `--port`       | `7000`        | bind port.                                                    |
| `--no-browser` | off           | skip the auto-open browser tab on localhost binds.            |
| `--log-level`  | `info`        | uvicorn log level (`debug` / `info` / `warning` / `error`).   |

The dashboard auto-refreshes the fleet table every 10 seconds via HTMX. Detail views are static, refresh manually.

## What it reads

The default reader walks `$ALFRED_HOME/state` (falling back to `~/.alfred/state` if the env var is unset). All reads are best-effort: missing directories render an empty state, malformed JSONL lines are skipped, the dashboard never throws.

If `$ALFRED_HOME/fleet-brain.db` exists, the reader also asks the fleet brain
for a read-only reliability report. Missing optional dependencies or a missing
brain database degrade to an "unknown" governor panel instead of failing the
page.

Canonical layout (written by `lib/agent_runner/`):

```
$ALFRED_HOME/state/
  <codename>/
    events/<firing_id>.jsonl     # one JSONL per firing
    spend-<YYYY-MM-DD>.json      # per-day per-codename ledger
  transcripts/<codename>/<YYYY-MM>/<firing_id>.jsonl
```

Forward-compatible optional paths the reader also honors if a future runtime writes them:

```
$ALFRED_HOME/state/codenames/<codename>/...
$ALFRED_HOME/state/firings/<firing_id>.json
```

Alfred plan drafts are read from:

```
$ALFRED_HOME/batman-plans/*.md
```

Planning drafts are written to:

```
$ALFRED_HOME/planning-drafts/*.md
$ALFRED_HOME/state/planning-drafts/*.json   # Slack listener intake
```

Registered Slack plan/report/draft threads are stored under:

```
$ALFRED_HOME/state/slack-threads/*.json
$ALFRED_HOME/state/slack-threads/feedback/*.jsonl
```

## Views

### `GET /` - Fleet status

Summary cards plus one row per codename:

- reliability-governor status and top action
- repeated failure-pattern count
- stale-worker count
- memory-promotion suggestions
- status dot (idle, live, error)
- last-run timestamp, rendered for scanning with the raw UTC value in the
  browser title
- firings-today count (read from the per-day spend ledger)
- last firing id (linked to the detail view) plus a one-line summary

The table auto-refreshes every 10 seconds via HTMX. The refresh swaps just the
table body, not the whole shell.

### `GET /firings` - Recent firings

The most recent 50 firings across all codenames, newest first. Each row links to its detail view.

Filters:

- `?codename=<name>` restricts the list to one codename. The clickable filter strip at the top of the page renders one link per known codename plus an "all" reset.

### `GET /plans` - Saved Alfred plans

Lists saved Alfred plan drafts from `$ALFRED_HOME/batman-plans`,
Slack/local planning JSON under `$ALFRED_HOME/state/planning-drafts`, and
Slack follow-up context under `$ALFRED_HOME/state/followups`. Each card shows
source, status, readiness score when present, revision count, affected repos,
parent issue, update time, and a local detail link.

### `GET /plans/{plan_id}` - Single saved plan

Renders the saved Markdown or generated spec body exactly as it exists on disk.
This keeps the local cockpit aligned with the Slack plan that the operator is
approving or editing.

For Slack follow-up items, the detail page also exposes two local actions:
`Plan next pass` converts the captured feedback into a scoped planning draft
under `$ALFRED_HOME/state/planning-drafts`, and `Mark handled` archives the
follow-up under `$ALFRED_HOME/state/followups/handled`. Neither action creates
a GitHub issue, opens a PR, approves work, or merges anything.

### `GET/POST /planning` - Issue/spec readiness

A local intake form for shaping work before Alfred files issues or opens PRs.
It turns title, problem, desired behavior, repo scope, acceptance criteria,
test plan, non-goals, rollout, and open questions into a GitHub-ready issue
draft.

The form also has a small planning-assistant box. You can type natural notes or
structured commands:

```text
acceptance: the Slack plan thread shows clear next steps before approval
test: add coverage for thread feedback parsing
add repo: my-org/mobile
remove repo: my-org/website
question: should approval happen after edits or after a new plan?
```

`Refine draft` applies those edits locally, re-runs readiness, and renders both
the GitHub issue draft and a spec draft. In Batman Slack approvals, the same
repo add/remove commands also amend execution scope before implementation.
Trusted replies after reports or PR links are captured during Batman's
report-feedback window and written to `$ALFRED_HOME/state/followups/` as
context for the next pass, never as merge approval. Those follow-ups are listed
in Plans with `needs follow-up` status. From the follow-up detail page, `Plan
next pass` turns that feedback into a local planning draft and archives the
original follow-up; `Mark handled` only archives it. `Save draft` writes the
issue body under `$ALFRED_HOME/planning-drafts`; `Save spec` writes the spec
body under `$ALFRED_HOME/spec-drafts`. None of these buttons create a GitHub
issue.

Slack-created drafts are saved as JSON under
`$ALFRED_HOME/state/planning-drafts/`. The dashboard treats that path as the
native-client draft inbox contract, so the local UI and Slack listener can
converge without inventing a second planning store.

When fleet-brain is available, the Planning page recalls a small number of
promoted lessons for the selected repos and shows them as planning memory.
Those hints are embedded in saved specs under a "Planning Memory" section, but
they never override the current issue or readiness checks. Saving a spec also
queues a reviewable memory candidate so useful spec-to-issue lessons can be
promoted explicitly.

By default refinement is deterministic and offline. Advanced operators can set
`ALFRED_PLANNING_ASSISTANT_ENGINE=<engine>` to let the configured local engine
rewrite the draft after the command parser has applied obvious edits. Use
`ALFRED_PLANNING_ASSISTANT_TIMEOUT` to cap that optional call.

The readiness panel blocks vague or incomplete work from feeling ready. It
asks concrete follow-up questions when problem, desired behavior, repo scope,
acceptance criteria, or test plan are missing. Open questions are a hard gate
until explicitly answered or accepted as risk.

### `GET /firings/{firing_id}` - Single firing detail

- meta (start, end, status, summary, events file path, transcript path if present)
- raw event stream rendered as JSON lines in a JetBrains Mono log strip

Returns 404 for unknown firing ids. The id is validated against path traversal before the reader touches disk.

### `GET /healthz`

Returns plain text `ok` with status 200. Useful for liveness probes if you run `alfred serve` behind a process supervisor.

### JSON API

The browser UI and future native client can read the same localhost data
through JSON endpoints:

```text
GET /api/status
GET /api/actions
GET /api/usage             # ships in an upcoming release; not served yet
GET /api/usage/providers   # ships in an upcoming release; not served yet
GET /api/firings?codename=<name>&limit=50
GET /api/firings/{firing_id}
GET /api/plans?limit=50
GET /api/plans/{plan_id}
POST /api/plans/{plan_id}/convert-followup
POST /api/plans/{plan_id}/mark-handled
GET /api/memory/candidates?status=candidate&limit=50
POST /api/memory/candidates/{candidate_id}/promote
POST /api/memory/candidates/{candidate_id}/reject
GET /api/slack/trusted-users
POST /api/slack/trusted-users
POST /api/slack/trusted-users/{user_id}/remove
```

Read endpoints intentionally mirror the HTML pages.

`GET /api/usage` and `GET /api/usage/providers` ship in an upcoming release; they
are not served by `alfred serve` yet. Once available they back the desktop
client's capacity rail. They report the operator's real Claude and Codex
subscription headroom for the rolling 5-hour and weekly windows, read from the
engines' own local CLI state files on the host. Alfred drives Claude Code and Codex through
their local subscription CLIs rather than API keys, so there is no billing API
and no per-token dollar figure (it is meaningless under a Max or Pro
subscription). A provider whose local state cannot be read degrades to
`available: false` with a reason rather than guessing, and any single window the
CLI does not persist reads as not synced rather than a fabricated number. Reads
run in a worker thread so filesystem work never stalls the event loop. The same
numbers are available from the command line with `alfred usage` (see
[`CLI.md`](CLI.md)).

The follow-up action
endpoints are local-file actions only: they convert captured feedback into a
planning draft JSON or archive it as handled. They do not call GitHub, Slack,
or an engine, and they do not approve execution. Memory candidate endpoints
only read or review rows in the local fleet-brain database. `promote` turns a
candidate into a recalled lesson, and `reject` keeps it out of future prompts.
Slack trusted-user endpoints only read or update
`$ALFRED_HOME/state/slack-trust/trusted-users.json`; they do not grant approval
rights, call Slack, call GitHub, or run an agent.

## Architecture

Three thin modules behind a single factory:

```
lib/server/
  __init__.py       # re-exports public surface
  reader.py         # FleetReader Protocol + FilesystemReader
  app.py            # create_app(reader) -> FastAPI
  formatting.py     # timestamp and firing-id presentation helpers
  views.py          # fleet, firings, plans, planning, detail, health routes
  templates/        # base + pages + 1 HTMX partial
  static/style.css  # Operations Room theme
bin/alfred-serve.py # argparse driver, runs uvicorn
```

The reader is injected into the FastAPI app via `create_app(reader)`. Tests pass a tmp-dir-backed `FilesystemReader` (or any stub matching the `FleetReader` Protocol), so the test suite never touches a real fleet.

## Security model

Default bind is `127.0.0.1`. Runtime-state routes are read-only. The Planning
page accepts a local `POST` only to write markdown drafts under
`$ALFRED_HOME/planning-drafts` and `$ALFRED_HOME/spec-drafts`. Follow-up actions
only move captured follow-up files into `handled/` or create local planning
draft JSON. Memory candidate actions only mutate the local fleet-brain
database. Slack collaborator actions only mutate the local trust JSON file.
They do not call GitHub or Slack. The planning assistant only calls a model provider when
`ALFRED_PLANNING_ASSISTANT_ENGINE` is explicitly set. The reader's path-traversal guard rejects firing ids containing
`/`, `\\`, or a leading `.` before any filesystem read.

That said: the dashboard surfaces repo URLs, file paths, and event payloads that may contain operator context. Treat `--host 0.0.0.0` like exposing the raw state directory over HTTP, only do it on a network you trust.

## Tests

```bash
pytest tests/test_server.py -q
```

Covers empty state, populated state via `tmp_path`, codename filter, HTMX
partial swap, 404 on unknown firing, path-traversal rejection, saved plan
listing, planning draft readiness/saving, timestamp formatting,
malformed-JSONL tolerance, Slack trusted-user API guards, and `/healthz`.
