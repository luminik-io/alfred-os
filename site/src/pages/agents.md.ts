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
    "Alfred is an open-source local runtime for autonomous coding agents that turn",
    "Slack requests, rough plans, specs, and GitHub issues into PRs while you are away. It",
    "coordinates Claude Code and Codex CLI sessions on a",
    "Mac mini, MacBook, or Linux box you choose: macOS via launchd, Linux via",
    "systemd. Each agent is a named role (Batman is the architect for",
    "multi-repo rollouts, Lucius is the senior developer, Drake is the planner,",
    "and Ras al Ghul is the reviewer) with its own prompt, schedule, and label",
    "discipline.",
    "You do not sit in front of Claude or Codex and prompt every",
    "step. Alfred keeps the loop moving until it has a pull request, a review",
    "finding, or a decision to bring back to Slack.",
    "",
    "Source: https://github.com/luminik-io/alfred-os",
    "License: MIT",
    "",
    "## What problem it solves",
    "",
    "Most coding agents wait for a human to type the next prompt. Alfred is",
    "built for engineering work that should keep moving without you",
    "at the keyboard: planned features, Slack follow-ups, tests, reviewer comments,",
    "dependency bumps, docs gaps, and multi-repo rollouts. A scheduler fires",
    "each agent at a configured cadence; the harness wraps every firing in",
    "a lock, preflight, spend cap, and an isolated git worktree.",
    "",
    "## How to install",
    "",
    "The full install guide lives at [" + (installEntry?.data.title ?? "Install") + "](" + url("getting-started/install") + ").",
    "Short version:",
    "",
    "```",
    "git clone https://github.com/luminik-io/alfred-os ~/code/alfred-os",
    "cd ~/code/alfred-os",
    "./bin/alfred-init.py",
    "alfred doctor",
    "alfred dry-run lucius",
    "```",
    "",
    "After `alfred doctor` reports green, agents pick up GitHub issues",
    "labeled `agent:implement` (or `agent:large-feature` for multi-repo",
    "bundles) and open pull requests with tests.",
    "",
    "## How to integrate as an agent",
    "",
    "Alfred is configuration-first; integration points are GitHub labels",
    "and Alfred's local state directory (`~/.alfred/state/`).",
    "",
    "- File a GitHub issue with the body fields Alfred expects (target",
    "  repo, goal, constraints, done-when) and label it `agent:implement`.",
    "- Drake can read plain-text specs or structured plans and file scoped",
    "  child issues when the work is ready; otherwise it asks for the missing details.",
    "- Lucius claims an `agent:implement` issue on its next firing,",
    "  opens a worktree, runs Claude or Codex, opens a PR, and flips the",
    "  label to `agent:pr-open`.",
    "- Ras al Ghul reviews the PR diff, runs tests, files inline comments,",
    "  and labels the PR `ready` or `needs-changes`.",
    "- Batman acts as the architect agent for multi-repo work via",
    "  `agent:large-feature` issues and a configurable Slack approval gate.",
    "",
    "Full lifecycle: [" + (conceptsEntry?.data.title ?? "Architecture") + "](" + url("concepts/architecture") + ").",
    "Alfred CLI reference: [" + (cliEntry?.data.title ?? "CLI") + "](" + url("reference/cli") + ").",
    "",
    "## What Alfred does NOT do",
    "",
    "- Does not upload your repos to a shared service. Alfred runs on your",
    "  Mac mini, MacBook, old Mac, or Linux machine.",
    "- Does not require an LLM API key. Alfred invokes your",
    "  Claude Code or Codex CLI subscriptions; spend is whatever the",
    "  subscription is.",
    "- Sends no repo names, code, prompts, titles, branches, or people in usage totals.",
    "- Does not act without an issue label or a Slack approval (where",
    "  configured). Batman, in particular, halts before filing child",
    "  issues until you react with the configured emoji.",
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
