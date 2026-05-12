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
   curl -fsSL https://luminik-io.github.io/alfred-os/ | grep -E 'Alfred-OS|Starlight'
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

The launch URL is intentionally the GitHub Pages project URL:

```text
https://luminik-io.github.io/alfred-os/
```

For a cleaner branded URL later, prefer a subdomain:

```text
https://alfred-os.luminik.io/
```

Recommended Cloudflare + GitHub Pages shape:

1. Add a DNS-only `CNAME` record in Cloudflare:

   ```text
   alfred-os.luminik.io -> luminik-io.github.io
   ```

2. Set the GitHub Pages custom domain to `alfred-os.luminik.io`.
3. Set repository variables:

   ```text
   ALFRED_OS_SITE_URL=https://alfred-os.luminik.io
   ALFRED_OS_SITE_BASE=/
   ```

4. Re-run the `Site` workflow and wait for GitHub Pages to issue the TLS
   certificate before enforcing HTTPS.

Keep the default project URL until the public launch settles. It avoids DNS,
TLS, and Cloudflare proxy coupling during the first announcement.
