# Telemetry

Alfred can send anonymous aggregate usage totals to an ingest endpoint you
configure. Those totals can power public counters so people can see how Alfred
is being used without exposing private work.

The reporter is enabled unless you opt out, but it sends only when
`ALFRED_TELEMETRY_URL` is set. Without an endpoint, the scheduled reporter exits
cleanly and creates no install id.

## Control

```sh
alfred telemetry status
alfred telemetry on --url https://your-worker.example.com/ingest
alfred telemetry off
```

`alfred telemetry on` writes the endpoint and re-enables reporting. `alfred
telemetry off` writes `ALFRED_TELEMETRY_ENABLED=0`. The scheduler row can stay
installed; with telemetry off or no endpoint configured, the reporter exits
cleanly and sends nothing.

If your collector uses an ingest token:

```sh
alfred telemetry on \
  --url https://your-worker.example.com/ingest \
  --token the-same-value-as-the-collector
```

## Payload

Once a day, Alfred posts this JSON to `ALFRED_TELEMETRY_URL`:

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
| `lines_changed` | Lifetime changed-line total when the local brain has line counts. Current local reporters send `0` because the brain stores file touches, not additions/deletions. |
| `loc_added` | Historical wire alias for `files_changed`. |

Alfred never sends repo names, file paths, code, prompts, PR titles, issue
titles, branch names, people, hostnames, or billing data.

## Collector

The bundled Cloudflare Worker lives in
[`telemetry/worker/`](../telemetry/worker/):

- `POST /ingest` stores one latest record per install id.
- `GET /stats` returns aggregate totals and the number of active installs that
  have sent a report.

The Worker derives totals by summing current install records. It does not keep
per-install history. Use `INGEST_TOKEN` when you want only your own machines to
write to the collector.

## Local Preview

To preview the payload without sending it:

```sh
python3 bin/proof-telemetry.py --dry-run
```

Dry-run still respects `ALFRED_TELEMETRY_ENABLED=0`.
