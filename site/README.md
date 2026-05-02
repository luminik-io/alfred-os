# Pennyworth docs site

Astro Starlight site for [pennyworth](https://github.com/luminik-io/pennyworth).

## Local dev

```sh
npm install
npm run dev
```

Open the URL Astro prints (default `http://localhost:4321/pennyworth/`).

## Build

```sh
npm run build
```

Output in `dist/`.

## Deploy

CI deploys on push to `main` via `.github/workflows/site.yml`. Output served at `https://luminik-io.github.io/pennyworth/`.

## Override the site URL

```sh
PENNYWORTH_SITE_URL=https://pennyworth.dev PENNYWORTH_SITE_BASE=/ npm run build
```

Useful when migrating off GitHub Pages onto a custom domain.

## Adding a page

1. Drop a `.md` or `.mdx` file under `src/content/docs/<section>/`.
2. Add a sidebar entry in `astro.config.mjs`.
3. `npm run dev` to preview.

## Editing existing pages

The "Edit page" link in the rendered sidebar opens the file at `https://github.com/luminik-io/pennyworth/edit/main/site/...`. Set up by `editLink` in `astro.config.mjs`.
