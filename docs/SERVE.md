# `alfred serve`

A small, localhost-only, read-only dashboard over `$ALFRED_HOME/state` and the
local fleet brain. Three core views, no auth, no writes. The operator's pane of
glass for "what is the fleet doing right now".

Status: v0.4.0 shipped the first dashboard. v0.4.1 adds reliability-governor
cards and action summaries as a cross-platform precursor to any future native
menu-bar UI.

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

Canonical layout (written by `lib/agent_runner.py`):

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

## Views

### `GET /` - Fleet status

Summary cards plus one row per codename:

- reliability-governor status and top action
- repeated failure-pattern count
- stale-worker count
- memory-promotion suggestions
- status dot (idle, live, error)
- last-run timestamp
- firings-today count (read from the per-day spend ledger)
- last firing id (linked to the detail view) plus a one-line summary

The table auto-refreshes every 10 seconds via HTMX. The refresh swaps just the
table body, not the whole shell.

### `GET /firings` - Recent firings

The most recent 50 firings across all codenames, newest first. Each row links to its detail view.

Filters:

- `?codename=<name>` restricts the list to one codename. The clickable filter strip at the top of the page renders one link per known codename plus an "all" reset.

### `GET /firings/{firing_id}` - Single firing detail

- meta (start, end, status, summary, events file path, transcript path if present)
- raw event stream rendered as JSON lines in a JetBrains Mono log strip

Returns 404 for unknown firing ids. The id is validated against path traversal before the reader touches disk.

### `GET /healthz`

Returns plain text `ok` with status 200. Useful for liveness probes if you run `alfred serve` behind a process supervisor.

## Architecture

Three thin modules behind a single factory:

```
lib/server/
  __init__.py       # re-exports public surface
  reader.py         # FleetReader Protocol + FilesystemReader
  app.py            # create_app(reader) -> FastAPI
  views.py          # three GET routes
  templates/        # base + 4 pages + 1 HTMX partial
  static/style.css  # Operations Room theme
bin/alfred-serve.py # argparse driver, runs uvicorn
```

The reader is injected into the FastAPI app via `create_app(reader)`. Tests pass a tmp-dir-backed `FilesystemReader` (or any stub matching the `FleetReader` Protocol), so the test suite never touches a real fleet.

## Security model

Default bind is `127.0.0.1`. Every route is a `GET`; nothing mutates state. The reader's path-traversal guard rejects firing ids containing `/`, `\\`, or a leading `.` before any filesystem read.

That said: the dashboard surfaces repo URLs, file paths, and event payloads that may contain operator context. Treat `--host 0.0.0.0` like exposing the raw state directory over HTTP, only do it on a network you trust.

## Tests

```bash
pytest tests/test_server.py -q
```

Covers empty state, populated state via `tmp_path`, codename filter, HTMX partial swap, 404 on unknown firing, path-traversal rejection, malformed-JSONL tolerance, and `/healthz`.
