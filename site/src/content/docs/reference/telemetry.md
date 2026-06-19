---
title: Telemetry
description: Opt-in usage counts and the public impact page.
---

Alfred runtime telemetry is off by default. A default install does not phone
home and does not create an install id.

When an operator opts in, Alfred sends a small daily usage-count payload to
the endpoint they configure:

- random install token
- lifetime PRs opened by Alfred
- lifetime PRs merged
- lifetime PRs that reached merged or closed state
- lifetime changed-file proxy

No repo names, branch names, PR titles, code, logs, prompts, usernames, hostnames,
or billing data are sent.

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

`alfred telemetry on` writes a managed block to `~/.alfredrc` and adds the
`alfred.proof-telemetry` scheduler row to the source checkout's
`launchd/agents.conf` when Alfred can identify it. Re-run `deploy.sh` after
changing scheduler rows.

`alfred telemetry off` writes `ALFRED_TELEMETRY_ENABLED=0`, removes the
scheduler row, and removes the ingest token from the managed block.

## Public Counter

The marketing site has an `/impact/` page that can show anonymous aggregate
totals from opted-in installs. The static build reads the public stats endpoint
from `PUBLIC_ALFRED_TELEMETRY_STATS_URL`.

When no stats endpoint is configured, or when totals are below the proof floor,
the page shows a neutral warm-up state.

The public proof board on `/impact/` is separate from anonymous telemetry. It is
generated from public GitHub metadata for `luminik-io/alfred-os`, so the site can
show real PR links, issue flow, additions, deletions, and changed files without
asking private installs to send that detail.

## Collector

The bundled collector lives in
[`telemetry/worker/`](https://github.com/luminik-io/alfred-os/tree/main/telemetry/worker).
It exposes:

- `POST /ingest` for opted-in hosts
- `GET /stats` for aggregate public totals

Use `INGEST_TOKEN` on the Worker and the matching `ALFRED_TELEMETRY_TOKEN` on
opted-in hosts when counter integrity matters.

Full implementation contract:
[`docs/TELEMETRY.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/TELEMETRY.md).
