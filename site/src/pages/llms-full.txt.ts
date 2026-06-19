import type { APIRoute } from "astro";
import { getCollection } from "astro:content";

// Served at /llms-full.txt: surface 2 of the llmstxt.org spec
// (https://llmstxt.org). Where /llms.txt is a curated table of contents
// with one link per page, /llms-full.txt is the whole site in a single
// markdown file so an LLM can ingest the entire documentation set in
// one fetch without crawling every URL.
//
// This is the natural companion to:
//   - /llms.txt: curated TOC (surface 1)
//   - /<page>.md: per-page mirror (surface 3, built by
//                              src/integrations/md-mirror.ts)
//   - /agents.md: AGENTS.md convention (Anthropic et al.)
//
// Source of truth: the Starlight `docs` content collection. Pages outside
// the collection (marketing landings, install/pricing/multi-repo) are
// covered by the per-page .md mirrors and excluded here to keep this
// file aimed at the documentation surface specifically.

// Section order mirrors /llms.txt and the sidebar.
const SECTIONS: { prefix: string; label: string }[] = [
  { prefix: "getting-started/", label: "Getting started" },
  { prefix: "concepts/", label: "Concepts" },
  { prefix: "guides/", label: "Guides" },
  { prefix: "reference/", label: "Reference" },
  { prefix: "about/", label: "About" },
];

export const GET: APIRoute = async ({ site }) => {
  const origin = site ?? new URL("https://alfred.luminik.io");
  const docs = await getCollection("docs");

  const url = (id: string) =>
    new URL(`${import.meta.env.BASE_URL}${id}/`.replace(/\/{2,}/g, "/"), origin).href;

  const root = docs.find((d) => d.id === "");
  const summary =
    root?.data.description ??
    "Coding agents that ship from your specs while you are away. Claude Code and Codex agents run by launchd or systemd on a machine you control.";

  const out: string[] = [
    "# Alfred: full documentation",
    "",
    `> ${summary}`,
    "",
    "Alfred is the open-source local runtime for coding agents that",
    "turn specs and GitHub issues into PRs while the operator is away. The host scheduler",
    "(launchd on macOS, systemd on Linux) fires",
    "each agent; the harness wraps every firing in a lock, preflight, spend",
    "cap, and an isolated git worktree.",
    "",
    `Source: https://github.com/luminik-io/alfred-os`,
    `Roadmap: https://github.com/luminik-io/alfred-os/blob/main/ROADMAP.md`,
    "",
    "---",
    "",
    "This file is the whole documentation set in one piece, intended for",
    "LLM ingestion. For a curated link-only index, see /llms.txt. For a",
    "single page as raw markdown, append `.md` to any docs URL.",
    "",
  ];

  // Walk by section so the file reads in the same order as the sidebar.
  for (const { prefix, label } of SECTIONS) {
    const entries = docs
      .filter((d) => d.id.startsWith(prefix))
      .sort((a, b) => a.id.localeCompare(b.id));
    if (entries.length === 0) continue;

    out.push("", `# ${label}`, "");
    for (const entry of entries) {
      out.push(
        "",
        `## ${entry.data.title}`,
        "",
        `Source: ${url(entry.id)}`,
        "",
      );
      if (entry.data.description) {
        out.push(`> ${entry.data.description}`, "");
      }
      // entry.body is the raw markdown source as authored in src/content/docs/**.md.
      // Strip the frontmatter (Astro already parsed it into entry.data) and emit
      // the prose. We use the raw body rather than rendering through MDX because
      // we want markdown-in / markdown-out for LLM ingestion: no React islands,
      // no compiled HTML, no Starlight-specific components.
      const body = entry.body ?? "";
      const noFrontmatter = body.replace(/^---[\s\S]*?---\s*/m, "").trim();
      if (noFrontmatter) {
        out.push(noFrontmatter, "");
      }
    }
  }

  return new Response(out.join("\n"), {
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
};
