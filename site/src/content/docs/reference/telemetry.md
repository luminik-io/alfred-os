---
title: Telemetry
description: Aggregate usage-count reference for Alfred.
---

Alfred can send anonymous aggregate usage totals to the public impact counter.
Reporting is enabled unless you opt out and uses Alfred's hosted collector by
default. Set `ALFRED_TELEMETRY_URL` only when you want a self-hosted collector.

## CLI

```sh
alfred telemetry status
alfred telemetry on
alfred telemetry off
```

If your collector requires a token:

```sh
alfred telemetry on \
  --url https://your-worker.example.com/ingest \
  --token the-same-value-as-the-collector
```

`alfred telemetry on` writes the hosted endpoint and schedules the reporter.
`alfred telemetry off` writes `ALFRED_TELEMETRY_ENABLED=0`. The scheduler row can
stay installed; with telemetry off, the reporter exits cleanly and sends
nothing.

## Payload

Sent once a day:

- random install token
- lifetime Alfred-authored PRs opened
- lifetime Alfred-authored PRs merged
- lifetime Alfred-authored PRs that reached merged or closed state
- lifetime issues with an `agent:*` label
- lifetime issues with an `agent:*` label that reached closed state
- lifetime changed-file count
- lifetime changed-line count when the local brain has line counts

No repo names, branch names, PR titles, issue titles, code, logs, prompts,
people, hostnames, or billing data are sent.

## Public Counter

Site counters use build-time seed totals and replace them with live aggregate
totals from Alfred's hosted collector. Set `PUBLIC_ALFRED_TELEMETRY_STATS_URL`
only when building a fork or private counter.

Build-time seed totals can include line counts from GitHub. Anonymous local
reporters send changed-line totals when the local fleet brain has cached GitHub
additions and deletions.

The public GitHub examples on `/impact/` are separate. They use public GitHub
metadata for `luminik-io/alfred-os`.

## Collector

The bundled collector lives in
[`telemetry/worker/`](https://github.com/luminik-io/alfred-os/tree/main/telemetry/worker).
It exposes:

- `POST /ingest` for install reports
- `POST /register` for per-install write tokens
- `GET /stats` for aggregate public totals

Use `REQUIRE_INSTALL_TOKEN=1` for per-install write tokens, or `INGEST_TOKEN`
with `ALFRED_TELEMETRY_TOKEN` when a private self-hosted counter should use one
shared token.

The hosted public counter uses a private trusted-reporter token for every public
Impact total. Anonymous reports can be accepted by the collector, but they
cannot move PR, issue, file, line, or machine totals.

Full implementation contract:
[`docs/TELEMETRY.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/TELEMETRY.md).
