import type { APIRoute } from "astro";
import { getCollection } from "astro:content";

// Served at /agents.md. The emerging AGENTS.md convention (Anthropic and
// others) for sites that want a self-contained, agent-readable description
// of what the project is, how to install it, and how to integrate. Mirrors
// llms.txt but in prose markdown aimed at an LLM agent reading once.
//
// llms.txt (link-rich index) and agents.md (prose intro) are complementary;
// crawlers and discovery tools look for both.

export const GET: APIRoute = async ({ site }) => {
  const origin = site ?? new URL("https://alfred.luminik.io");
  const docs = await getCollection("docs");

  // Build-time base so a fork under a project sub-path emits correct URLs.
  const url = (id: string) =>
    new URL(`${import.meta.env.BASE_URL}${id}/`.replace(/\/{2,}/g, "/"), origin).href;
  const installEntry = docs.find((d) => d.id === "getting-started/install");
  const conceptsEntry = docs.find((d) => d.id === "concepts/architecture");
  const cliEntry = docs.find((d) => d.id === "reference/cli");

  const lines: string[] = [
    "# Alfred (agents.md)",
    "",
    "If you are an AI agent reading this file, here is what Alfred is, why",
    "the project exists, and how to install or integrate it. This is the",
    "agent-facing companion to [/llms.txt](" + url("llms.txt").replace(/\/$/, "") + ").",
    "",
    "## What Alfred is",
    "",
    "Alfred is an open-source runtime for a fleet of autonomous coding",
    "agents. It coordinates Claude Code and Codex CLI sessions on a",
    "machine the operator already owns: macOS via launchd, Linux via",
    "systemd. Each agent is a named role (Drake triages specs, Lucius",
    "implements scoped issues, Ras al Ghul reviews PRs, Batman coordinates",
    "multi-repo rollouts) with its own prompt, schedule, and label",
    "discipline.",
    "",
    "Source: https://github.com/luminik-io/alfred-os",
    "License: MIT",
    "",
    "## What problem it solves",
    "",
    "Most coding agents wait for a human to type a prompt. Alfred is built",
    "for the work that keeps coming after the keyboard closes: planned",
    "features, follow-up tests, reviewer comments, dependency bumps, docs",
    "gaps, and multi-repo rollouts. A scheduler fires each agent at a",
    "configured cadence; the harness wraps every firing in a lock,",
    "preflight, spend cap, and an isolated git worktree.",
    "",
    "## How to install",
    "",
    "The full install guide lives at [" + (installEntry?.data.title ?? "Install") + "](" + url("getting-started/install") + ").",
    "Short version:",
    "",
    "```",
    "git clone https://github.com/luminik-io/alfred-os ~/code/alfred-os",
    "cd ~/code/alfred-os",
    "bash bin/alfred-init",
    "alfred doctor",
    "alfred fire lucius --dry-run",
    "```",
    "",
    "After `alfred doctor` reports green, agents pick up GitHub issues",
    "labeled `agent:implement` (or `agent:large-feature` for multi-repo",
    "bundles) and open pull requests with tests.",
    "",
    "## How to integrate as an agent",
    "",
    "Alfred is configuration-first; integration points are GitHub labels",
    "and the operator's local state directory (`~/.alfred/state/`).",
    "",
    "- File a GitHub issue with the body fields Alfred expects (target",
    "  repo, goal, constraints, done-when) and label it `agent:implement`.",
    "- Drake will read `/specs/*.md` and file scoped child issues if the",
    "  spec is well-formed; otherwise it pings the operator for the gaps.",
    "- Lucius claims an `agent:implement` issue on its next firing,",
    "  opens a worktree, runs Claude or Codex, opens a PR, and flips the",
    "  label to `agent:pr-open`.",
    "- Ras al Ghul reviews the PR diff, runs tests, files inline comments,",
    "  and labels the PR `ready` or `needs-changes`.",
    "- Batman coordinates multi-repo work via `agent:large-feature` issues",
    "  and a configurable Slack approval gate.",
    "",
    "Full lifecycle: [" + (conceptsEntry?.data.title ?? "Architecture") + "](" + url("concepts/architecture") + ").",
    "Operator CLI reference: [" + (cliEntry?.data.title ?? "CLI") + "](" + url("reference/cli") + ").",
    "",
    "## What Alfred does NOT do",
    "",
    "- Does not run a managed control plane. Everything runs on the",
    "  operator's machine.",
    "- Does not require an LLM API key. Alfred invokes the operator's",
    "  Claude Code or Codex CLI subscriptions; spend is whatever the",
    "  subscription is.",
    "- Does not call out to a vendor backend for telemetry.",
    "- Does not act without an issue label or a Slack approval (where",
    "  configured). Batman, in particular, halts before filing child",
    "  issues until the operator reacts with the configured emoji.",
    "",
    "## How to crawl the rest of the site",
    "",
    "- Link-rich index for LLMs: [/llms.txt](" + url("llms.txt").replace(/\/$/, "") + ")",
    "- Sitemap: " + new URL(`${import.meta.env.BASE_URL}sitemap-index.xml`.replace(/\/{2,}/g, "/"), origin).href,
    "- GitHub repo: https://github.com/luminik-io/alfred-os",
    "- Roadmap: https://github.com/luminik-io/alfred-os/blob/main/ROADMAP.md",
    "",
    "## Contact",
    "",
    "Open an issue on GitHub. Alfred is built and maintained by Prasad",
    "Subrahmanya (https://prasad.tech, https://github.com/prasadus92).",
    "",
  ];

  return new Response(lines.join("\n"), {
    headers: { "Content-Type": "text/markdown; charset=utf-8" },
  });
};
