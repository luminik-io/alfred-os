---
title: Publishing
description: GitHub Pages release checks, custom-domain setup, and the README/Jekyll fallback fix.
---

Alfred-OS publishes this docs site through the `Site` workflow. The repo should
use GitHub Actions as the Pages source, not the legacy branch/root Pages
builder.

## Required repo variables

Set this before public launch:

```text
ALFRED_OS_PUBLISH_PAGES=true
```

Optional custom-domain variables:

```text
ALFRED_OS_SITE_URL=https://alfred.luminik.io
ALFRED_OS_SITE_BASE=/
```

## Verify Pages mode

```sh
gh api repos/luminik-io/alfred-os/pages --jq '{build_type,status,html_url}'
```

Expected:

```json
{"build_type":"workflow","status":"built","html_url":"https://alfred.luminik.io/"}
```

If the page shows the README instead of this Starlight site, switch Pages to
workflow mode and dispatch a fresh deploy:

```sh
gh api --method PUT repos/luminik-io/alfred-os/pages -f build_type=workflow
gh workflow run site.yml --repo luminik-io/alfred-os --ref main
```

GitHub's CDN can keep the old page briefly. Hard refresh if the browser still
has the branch-built page cached.

## Custom domain

For a branded URL, prefer:

```text
https://alfred.luminik.io/
```

In Route53, add a `CNAME` from `alfred.luminik.io` to
`luminik-io.github.io`, set the same custom domain in GitHub Pages, then set
`ALFRED_OS_SITE_URL=https://alfred.luminik.io` and
`ALFRED_OS_SITE_BASE=/`.

GitHub can take a few minutes to issue the TLS certificate. Keep HTTPS
unenforced until GitHub reports the certificate is ready.
