# Input connectors

Alfred's engineering fleet runs off a GitHub issue queue: any issue
labeled `agent:implement` is fair game. **Connectors** let you feed that
queue from non-GitHub sources (Linear tickets, Sentry alerts, and
future systems) without changing how the agents themselves work.

## Mental model

```
+----------+        +---------------------+        +--------+
|  Linear  | -----> | LinearConnector     | --+    |        |
+----------+        +---------------------+   |    |  gh    |
                                              +-->-+ issue  | --> agent:implement
+----------+        +---------------------+   |    | create |     queue
|  Sentry  | -----> | SentryConnector     | --+    |        |
+----------+        +---------------------+        +--------+
```

The runner is the single side-effect boundary. Connectors only know how
to **poll their source** and emit normalized `IssueDraft` records;
filing the GitHub issue is the runner's job.

## Pull-mode (now) vs push-mode (v2)

Connectors are **pull-mode**: the operator runs `connector-sync` on a
launchd / systemd timer (every 10-30 min is typical) and each
connector polls its upstream API.

Webhook **push-mode** is deferred to v2:

| Dimension              | Pull (today)                         | Push (v2)                                     |
| ---------------------- | ------------------------------------ | --------------------------------------------- |
| Operator infra         | None, `connector-sync` on a timer    | Public HTTPS endpoint + signature verification |
| Latency                | Up to one polling interval           | Seconds                                       |
| Volume                 | Bounded by `page_size` per poll      | Unbounded; needs back-pressure                |
| Failure mode           | Retry on next tick, idempotent       | Webhook receiver must dedup + retry           |
| Setup friction         | Drop a YAML entry, set env var       | Each source's webhook config + HMAC secret    |
| Fits single-operator   | Yes, local box can poll on a timer   | Needs a publicly reachable address            |

The pull-mode design ships value today on a single box. Push-mode is on
the roadmap once Alfred has a daemon worth pointing webhooks at.

## Anatomy of a connector

Every connector implements `lib/connectors/__init__.py::Connector`:

```python
class Connector(Protocol):
    name: str
    default_labels: list[str]
    default_repo: str | None
    def poll(self, since: datetime | None) -> list[IssueDraft]: ...
    def mark_seen(self, draft: IssueDraft) -> None: ...
```

and emits `IssueDraft`:

```python
@dataclass
class IssueDraft:
    source: str
    source_id: str
    title: str
    body: str
    labels: list[str]
    severity: Literal["info", "warning", "blocker"]
    target_repo: str | None
    source_url: str
```

The runner:

1. Reads the seen-cache at `$ALFRED_HOME/state/connectors/<name>.json`.
2. Calls `connector.poll(since=<last_poll_at>)`.
3. Skips any draft whose `source_id` is already in the cache.
4. For each new draft, runs `gh issue create -R <repo>` with the merged
   labels: `[agent:implement, connector, *connector.default_labels,
   *draft.labels, connector:<severity>]`.
5. On success, adds `source_id` to the seen-cache and calls
   `connector.mark_seen(draft)`.
6. Persists `last_poll_at` so the next run is incremental.

## Operator setup

### Configure

Edit `examples/connectors.yaml` (or point `ALFRED_CONNECTORS_CONFIG` at
your own copy). Sample shape:

```yaml
connectors:
  - name: linear
    type: linear
    enabled: true
    api_key_env: LINEAR_API_KEY
    default_repo: example-org/example-backend
    default_labels: [source:linear]
    filter:
      team_key: ENG
      state: Ready
      label: agent-ready

  - name: sentry
    type: sentry
    enabled: true
    api_key_env: SENTRY_AUTH_TOKEN
    organization: example-org
    project: example-web
    min_severity: warning
    default_repo: example-org/example-web
```

### Set env vars

API keys are **env-only**. Connectors refuse to load an inline value:

```sh
export LINEAR_API_KEY=lin_api_xxxxxxxxxxxx
export SENTRY_AUTH_TOKEN=sntrys_xxxxxxxxxxxxxxxxxxxxxxxx
```

### Run

```sh
# Dry-run the full pipe (narrates would-be `gh issue create` calls),
# updates the seen-cache so a subsequent live run won't re-fire.
./bin/connector-sync.py --dry-run

# Live run, all enabled connectors.
./bin/connector-sync.py

# Subset.
./bin/connector-sync.py --connectors linear

# Machine-readable report.
./bin/connector-sync.py --json
```

### Schedule it

Add to `launchd/agents.conf` (macOS) or the systemd equivalent:

```
my.fleet.connector-sync	connector-sync.py	interval:900	no		Input connector poll
```

15-minute cadence is a reasonable starting default.

## IssueDraft -> GitHub issue mapping

| `IssueDraft` field | `gh issue create` flag      | Notes                                                    |
| ------------------ | --------------------------- | -------------------------------------------------------- |
| `title`            | `--title`                   | Truncated to 200 chars; newlines collapsed.              |
| `body`             | `--body`                    | Footer appended (see below).                             |
| `labels` + ctor    | `--label` (one per)         | Stacked: `agent:implement`, `connector`, defaults, draft labels, `connector:<severity>`. |
| `severity`         | `--label connector:<tier>`  | `info` / `warning` / `blocker`.                          |
| `target_repo`      | `-R`                        | Falls back to `connector.default_repo`.                  |
| `source_url`       | Body footer                 | Linked under a `---` rule with `source` + `source_id`.   |
| `source_id`        | Seen-cache key              | Persisted at `$ALFRED_HOME/state/connectors/<name>.json`. |

Rendered footer:

```
---
Filed by Alfred connector `linear` from `ENG-101`.
Source: https://linear.app/example/issue/ENG-101
```

## Writing a new connector

Open-closed by construction: a new source is a new file, no edits to
the runner or other connectors.

1. Create `lib/connectors/myservice.py`.
2. Implement the `Connector` Protocol. Use `UrllibHttpClient` (or any
   stdlib transport) wrapped behind the `HttpClient` Protocol so tests
   can inject a fake.
3. Register the factory in `bin/connector-sync.py::CONNECTOR_FACTORIES`.
4. Add tests under `tests/test_myservice_connector.py` using the same
   `FakeHttp` pattern as the Linear / Sentry tests.
5. Document the config keys in this file.

Keep each connector to roughly 150 lines. If you need cycle handling,
parent-issue traversal, or richer routing, write a separate
`myservice_advanced.py` rather than expanding the reference impl.

## Hard rules

- **API keys are env-only**. No file storage. Connectors refuse to load
  an inline value.
- **Zero new third-party deps**. The reference connectors use stdlib
  `urllib.request`. PyYAML is *optional*: `connector-sync` falls back
  to a small handwritten YAML subset when it is not installed.
- **Dedup is per-connector** on `source_id`. The runner skips already-seen
  drafts before any `gh` call.
- **One bad connector cannot kill the rest**. Each runs inside its own
  try/except inside the runner.

## Deferred items

- **Push-mode (webhooks)**. Trade-off matrix above.
- **More connectors out of the box**: Datadog, PagerDuty, GitHub
  Discussions, Slack `/alfred new-issue`, Calendly no-shows.
- **Attachment handling**. Today the body is plain Markdown; uploading
  Sentry stack traces or Linear attachments as `gh issue attach` would
  let agents read full context. Needs `gh api` plumbing + size limits.
- **Two-way sync**. Closing a GitHub issue could mark the source
  resolved. Out of scope for v1.
- **Per-connector rate-limit awareness** beyond the runner's polling
  interval. The current design assumes operator-chosen cadence is the
  primary rate-limiter.
