import type { APIRoute } from "astro";
import { getCollection } from "astro:content";

// Served at /llms.txt: the llmstxt.org convention, a curated, link-rich
// markdown index an LLM can read to understand the site. Generated from the
// Starlight `docs` collection so it stays in sync as pages are added.

// Section order + labels mirror the docs-site sidebar. Anything whose id does
// not start with one of these prefixes is skipped from the grouped lists; the
// root page (id "") is handled separately as the intro.
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

  // Page URLs must include the configured `base` path (ALFRED_OS_SITE_BASE),
  // not just the origin. A fork hosting under e.g. /docs/ needs every link
  // prefixed so crawlers and LLMs following llms.txt hit the right paths.
  // import.meta.env.BASE_URL is the build-time `base`, always "/"-bounded.
  const url = (id: string) =>
    new URL(`${import.meta.env.BASE_URL}${id}/`.replace(/\/{2,}/g, "/"), origin).href;
  const root = docs.find((d) => d.id === "");
  const summary =
	    root?.data.description ??
    "Autonomous coding agents that keep development moving while you are away. Claude Code and Codex agents run by launchd or systemd on a machine you control.";

  const lines: string[] = [
    "# Alfred",
    "",
    `> ${summary}`,
    "",
    "Alfred is the open-source local runtime for autonomous coding agents that",
    "turn Slack requests, rough plans, specs, and GitHub issues into PRs while the operator is away. The host scheduler",
    "(launchd on macOS, systemd on Linux) fires",
    "each agent; the harness wraps every firing in a lock, preflight, spend",
    "cap, and an isolated git worktree. The engineering fleet ships today;",
    "content, sales, and ops departments are the roadmap. Source: https://github.com/luminik-io/alfred-os",
    "",
  ];

  for (const { prefix, label } of SECTIONS) {
    const entries = docs
      .filter((d) => d.id.startsWith(prefix))
      .sort((a, b) => a.id.localeCompare(b.id));
    if (entries.length === 0) continue;
    lines.push(`## ${label}`, "");
    for (const e of entries) {
      const desc = e.data.description ? `: ${e.data.description}` : "";
      lines.push(`- [${e.data.title}](${url(e.id)})${desc}`);
    }
    lines.push("");
  }

  lines.push(
    "## Source",
    "",
    "- [GitHub repository](https://github.com/luminik-io/alfred-os): the framework, examples, and issues.",
    "- [Roadmap](https://github.com/luminik-io/alfred-os/blob/main/ROADMAP.md): shipped, in flight, and the design boundaries.",
    "",
    "## Other LLM-friendly surfaces",
    "",
    `- [${new URL("llms-full.txt", origin).href}](${new URL("llms-full.txt", origin).href}): the entire documentation set in a single markdown file.`,
    `- [${new URL("agents.md", origin).href}](${new URL("agents.md", origin).href}): the AGENTS.md convention; prose intro for an agent reading once.`,
    "- Per-page markdown mirrors: append `.md` to any page URL (e.g. `/getting-started/install.md`) to fetch that page as raw GFM markdown.",
    "",
  );

  return new Response(lines.join("\n"), {
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
};
