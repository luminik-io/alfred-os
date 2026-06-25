# Telemetry

Alfred can send anonymous aggregate usage totals to the public
[Impact](https://alfred.luminik.io/impact/) counter. Those totals show what
Alfred installs are shipping without exposing private work.

The reporter is enabled unless you opt out. It uses Alfred's hosted collector by
default. Set `ALFRED_TELEMETRY_URL` only when you want a self-hosted collector.

## Control

```sh
alfred telemetry status
alfred telemetry on
alfred telemetry off
```

`alfred telemetry on` writes the hosted endpoint and adds the reporter's
scheduler row. `alfred telemetry off` asks the collector to remove this
install's previous record, then writes `ALFRED_TELEMETRY_ENABLED=0`. The
scheduler row can stay installed; with telemetry off, the reporter exits
cleanly and sends nothing.

After changing the telemetry config, load the reporter into the host scheduler
(`launchd` on macOS, `systemd --user` on Linux). A Homebrew install exposes the
wrapper as `alfred-deploy`; from a source checkout run `bash deploy.sh`. Until
then the row is recorded but no unit is loaded, so nothing is sent.

Self-hosted collector:

```sh
alfred telemetry on \
  --url https://your-worker.example.com/ingest \
  --token the-same-value-as-the-collector
alfred-deploy        # Homebrew install; or `bash deploy.sh` from a source checkout
```

## Payload

Once a day, Alfred posts this JSON:

```json
{
  "install_id": "a-random-opaque-token",
  "period": "lifetime",
  "prs_opened": 42,
  "prs_merged": 31,
  "prs_reviewed": 18,
  "issues_opened": 19,
  "issues_closed": 14,
  "files_changed": 1287,
  "lines_changed": 0,
  "loc_added": 1287
}
```

| Field | Meaning |
| --- | --- |
| `install_id` | Random local token stored at `$ALFRED_HOME/state/telemetry-install-id`. It lets the collector replace the same install's latest report instead of double-counting it. |
| `period` | Always `lifetime`. |
| `prs_opened` | Lifetime Alfred-authored PRs cached in the local fleet brain. |
| `prs_merged` | Lifetime Alfred-authored PRs that merged. |
| `prs_reviewed` | Lifetime Alfred-authored PRs that reached merged or closed state. |
| `issues_opened` | Lifetime issues with an `agent:*` label. |
| `issues_closed` | Lifetime issues with an `agent:*` label that reached closed state. |
| `files_changed` | Lifetime file-touch count from the local fleet brain. |
| `lines_changed` | Lifetime additions plus deletions from cached Alfred-authored GitHub PRs when the local brain has line counts. |
| `loc_added` | Historical wire alias for `files_changed`. |

Alfred never sends repo names, file paths, code, prompts, PR titles, issue
titles, branch names, people, hostnames, or billing data.

## Collector

The bundled Cloudflare Worker lives in
[`telemetry/worker/`](../telemetry/worker/):

- `POST /ingest` stores one latest record per verified install id. A tombstone
  payload removes that install's record when the user turns telemetry off.
- `POST /register` issues the per-install write token used by Alfred's hosted
  collector. The token is stored locally at `$ALFRED_HOME/state/telemetry-token`.
- `GET /stats` returns the public aggregate totals.

The Worker derives totals by summing current install records. It does not keep
per-install history. Use `REQUIRE_INSTALL_TOKEN=1` for per-install tokens, or
`INGEST_TOKEN` when you want one shared token for a private self-hosted
collector.

For the hosted public counter, Alfred keeps every public Impact total behind a
trusted collector token. Anonymous reports can be accepted by the hosted
collector, but they do not move public PR, issue, file, line, or machine totals.

## Integrity and trust

Untrusted reports cannot move the public number; only the trusted collector
token can, and it isn't distributed. The protections:

- **Per-install write tokens.** `POST /register` mints a random token and stores
  only its SHA-256 hash, so one install can replace only its own record.
- **Trusted-counts gate.** The hosted collector runs with
  `TRUSTED_COUNTS_ONLY=1`, so public totals move only for reports carrying the
  trusted collector token, which never ships in the client.
- **Derive on read.** `GET /stats` recomputes the public total from the current
  trusted install records, so a replayed report never double-counts and a
  tombstone cleanly removes an install. Anomaly clamps and per-source rate
  limiting bound any single source as defense in depth.

Forks that do not set `TRUSTED_COUNTS_ONLY` accept open reports by design and
should treat their own totals accordingly.

## Local Preview

To preview the payload without sending it:

```sh
python3 bin/proof-telemetry.py --dry-run
```

Dry-run still respects `ALFRED_TELEMETRY_ENABLED=0`.
