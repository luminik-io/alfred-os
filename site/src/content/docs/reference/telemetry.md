---
title: Telemetry
description: Aggregate usage-count reference for Alfred.
---

Alfred can send anonymous aggregate usage totals to an ingest endpoint you
configure. Reporting is enabled unless you opt out, but nothing is sent until
`ALFRED_TELEMETRY_URL` exists. The totals can power public counters that show
how Alfred is being used without exposing private work.

## CLI

```sh
alfred telemetry status
alfred telemetry on --url https://your-worker.example.com/ingest
alfred telemetry off
```

If your collector requires a token:

```sh
alfred telemetry on \
  --url https://your-worker.example.com/ingest \
  --token the-same-value-as-the-collector
```

`alfred telemetry off` writes `ALFRED_TELEMETRY_ENABLED=0` and removes the
scheduler row. `alfred telemetry on` writes the endpoint, re-enables reporting,
and schedules `alfred.proof-telemetry`.

## Payload

Sent once a day when an endpoint is configured:

- random install token
- lifetime Alfred-authored PRs opened
- lifetime Alfred-authored PRs merged
- lifetime Alfred-authored PRs that reached merged or closed state
- lifetime issues with an `agent:*` label
- lifetime changed-file count
- lifetime changed-line count when the local brain has line counts

No repo names, branch names, PR titles, issue titles, code, logs, prompts,
people, hostnames, or billing data are sent.

## Public Counter

Site counters can use build-time seed totals and replace them with live
aggregate totals from `PUBLIC_ALFRED_TELEMETRY_STATS_URL`.

Build-time seed totals can include line counts from GitHub. Anonymous local
reporters send `lines_changed: 0` until Alfred stores per-line
additions/deletions in the local fleet brain.

The public GitHub examples on `/impact/` are separate. They use public GitHub
metadata for `luminik-io/alfred-os`.

## Collector

The bundled collector lives in
[`telemetry/worker/`](https://github.com/luminik-io/alfred-os/tree/main/telemetry/worker).
It exposes:

- `POST /ingest` for reporting installs
- `GET /stats` for aggregate public totals

Use `INGEST_TOKEN` on the Worker and the matching `ALFRED_TELEMETRY_TOKEN` on
reporting installs when only your own hosts should write to the counter.

Full implementation contract:
[`docs/TELEMETRY.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/TELEMETRY.md).
