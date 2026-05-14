import type { APIRoute } from "astro";

// Served at /robots.txt. Allows all crawlers and points them at the
// Starlight-generated sitemap index. The URL is resolved from both the
// configured origin (`site` / ALFRED_OS_SITE_URL) and the configured base
// path (import.meta.env.BASE_URL / ALFRED_OS_SITE_BASE), so a fork deploying
// under its own domain *and* a project sub-path gets a correct Sitemap line.
export const GET: APIRoute = ({ site }) => {
  const sitemapPath = `${import.meta.env.BASE_URL}sitemap-index.xml`.replace(/\/{2,}/g, "/");
  const sitemap = site ? new URL(sitemapPath, site).href : sitemapPath;
  const body = `User-agent: *
Allow: /

Sitemap: ${sitemap}
`;
  return new Response(body, {
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
};
