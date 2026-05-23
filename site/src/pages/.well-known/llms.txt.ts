import type { APIRoute } from "astro";

// Served at /.well-known/llms.txt. Discovery tools that follow the
// .well-known/ convention (RFC 8615) check here as well as /llms.txt.
// Redirect to the canonical /llms.txt so there is exactly one body to
// maintain. 308 preserves method + body for any non-GET probe.
export const GET: APIRoute = ({ site, redirect }) => {
  const origin = site ?? new URL("https://alfred.luminik.io");
  const target = new URL(
    `${import.meta.env.BASE_URL}llms.txt`.replace(/\/{2,}/g, "/"),
    origin,
  ).href;
  return redirect(target, 308);
};
