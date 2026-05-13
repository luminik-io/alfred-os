# Publishing

This repo publishes documentation through GitHub Pages using the `Site`
workflow. It should not use the legacy "Deploy from a branch" Pages builder.

## Public Launch Checklist

1. Make the repository public.
2. In GitHub repository variables, set:

   ```text
   ALFRED_OS_PUBLISH_PAGES=true
   ```

3. Configure GitHub Pages:

   ```sh
   gh api --method PUT repos/luminik-io/alfred-os/pages -f build_type=workflow
   gh api repos/luminik-io/alfred-os/pages --jq '{build_type,status,html_url,https_enforced}'
   ```

   Expected `build_type`: `workflow`.

4. Dispatch the site workflow:

   ```sh
   gh workflow run site.yml --repo luminik-io/alfred-os --ref main
   gh run list --repo luminik-io/alfred-os --workflow Site --limit 3
   ```

5. Verify the deployed site:

   ```sh
   curl -fsSL https://alfred.luminik.io/ | grep -E 'Alfred|Starlight'
   ```

## Common Failure: README Site

If `https://luminik-io.github.io/alfred-os/` shows a plain README/Jekyll page
instead of the Starlight docs site, Pages is using the legacy branch/root
builder. Switch the source to GitHub Actions:

```sh
gh api --method PUT repos/luminik-io/alfred-os/pages -f build_type=workflow
gh workflow run site.yml --repo luminik-io/alfred-os --ref main
```

GitHub's CDN can cache the previous page for a few minutes. A hard refresh or a
new browser session usually confirms the fixed deploy.

## Custom Domain

The launch URL is:

```text
https://alfred.luminik.io/
```

Current DNS shape:

```text
alfred.luminik.io -> luminik-io.github.io
```

Recommended Route53 + GitHub Pages checks:

1. Ensure the Route53 record exists:

   ```sh
   dig +short alfred.luminik.io CNAME
   ```

2. Ensure GitHub Pages has the same custom domain:

   ```sh
   gh api repos/luminik-io/alfred-os/pages --jq '{cname,html_url,https_enforced}'
   ```

3. Set repository variables:

   ```text
   ALFRED_OS_SITE_URL=https://alfred.luminik.io
   ALFRED_OS_SITE_BASE=/
   ```

4. Re-run the `Site` workflow and wait for GitHub Pages to issue the TLS
   certificate before enforcing HTTPS.
