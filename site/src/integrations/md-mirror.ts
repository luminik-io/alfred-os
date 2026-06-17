/**
 * md-mirror — Astro integration that emits a per-page Markdown sibling
 * for every built HTML page. Surface 3 of the llmstxt.org pattern (after
 * /llms.txt and /llms-full.txt): AI crawlers (ChatGPT, Claude, Perplexity,
 * GPTBot, ClaudeBot, PerplexityBot) can ingest each page as raw GFM
 * markdown instead of converting our HTML themselves.
 *
 * Runs at `astro:build:done`: walks every `.html` in `dist/`, extracts
 * the `<main>` content region (or, if absent, the `<article>`), strips
 * any element marked `data-md-skip`, converts to markdown via
 * node-html-markdown, and writes BOTH:
 *
 *   - colocated:   dist/<path>/index.html  ->  dist/<path>/index.md
 *   - append-.md:  dist/<path>/index.html  ->  dist/<path>.md
 *
 * The append form is the one most AI crawlers default to (they probe
 * `<url>.md` directly); the colocated form is a courtesy for tooling
 * that prefers it (some markdown-aware editors, GitHub-style routing).
 *
 * Opt-outs:
 *   - Whole page: list its URL path (no leading or trailing slash) in
 *     the integration `excludePaths` array. E.g. `["404"]`.
 *   - Component-level: add `data-md-skip` to the root element of an
 *     island, illustration, or chrome block. The integration strips
 *     it (and its children) before conversion.
 *
 * Why a build-time integration instead of an [...slug].md.ts endpoint:
 *   - Coverage: handles content collections (Starlight `docs`) AND
 *     custom .astro marketing pages in one shot.
 *   - Future-proof: every new .astro page mirrors automatically on
 *     the next build, no per-page wiring.
 *   - No endpoint sprawl: no [...slug].md.ts to keep in lockstep with
 *     content-collection schema changes.
 *
 * The shape (walk-dist + extract-main + node-html-markdown + colocated
 * `.md` siblings + `data-md-skip` opt-out) follows the pattern landed on
 * an internal Astro project; this OSS version drops the Cloudflare
 * `_headers` append because alfred-os deploys to GitHub Pages, which
 * serves `.md` with the correct Content-Type from its built-in MIME map.
 */

import { readdir, readFile, writeFile } from "node:fs/promises";
import { join, relative, dirname, basename } from "node:path";
import { fileURLToPath } from "node:url";
import type { AstroIntegration } from "astro";
import { NodeHtmlMarkdown } from "node-html-markdown";

interface Options {
  /**
   * URL paths to skip (no leading or trailing slash). These pages will
   * NOT get a .md mirror. Typical entries: "404", redirect shims, pages
   * whose content is purely interactive and would convert to nothing
   * useful.
   */
  excludePaths?: string[];
}

const DEFAULT_EXCLUDE = ["404"];

export function mdMirror(opts: Options = {}): AstroIntegration {
  const exclude = new Set([...(opts.excludePaths ?? []), ...DEFAULT_EXCLUDE]);
  return {
    name: "md-mirror",
    hooks: {
      "astro:build:done": async ({ dir, logger }) => {
        const distDir = fileURLToPath(dir);
        const htmlFiles = await listHtmlFiles(distDir);
        const nhm = new NodeHtmlMarkdown(
          {
            keepDataImages: false,
            useLinkReferenceDefinitions: false,
            useInlineLinks: true,
            maxConsecutiveNewlines: 2,
          },
          undefined,
          undefined,
        );

        let written = 0;
        let skipped = 0;
        for (const abs of htmlFiles) {
          // Convert `dist/getting-started/install/index.html` -> path slug
          // `getting-started/install`. Skip directory indexes only; pages
          // that are NOT `index.html` (rare in Astro) get their own slug.
          const rel = relative(distDir, abs);
          const slug = htmlPathToSlug(rel);
          if (exclude.has(slug)) {
            skipped++;
            continue;
          }

          const html = await readFile(abs, "utf8");
          const main = extractMain(html);
          if (!main) {
            // No <main> or <article> — site chrome only, nothing to mirror.
            skipped++;
            continue;
          }
          const cleaned = stripSkipMarked(main);
          const md = nhm.translate(cleaned).trim() + "\n";

          // Write both colocated and append-.md siblings. For the root
          // index.html, the append-.md form would be just `.md` (empty
          // basename); we skip it for root and rely on /llms-full.txt
          // as the canonical "whole site" markdown surface.
          const colocated = abs.replace(/\.html$/, ".md");
          await writeFile(colocated, md, "utf8");
          written++;

          if (rel === "index.html") continue;
          if (basename(rel) === "index.html") {
            const dirPath = dirname(abs);
            const parent = dirname(dirPath);
            const dirName = basename(dirPath);
            const appendForm = join(parent, dirName + ".md");
            await writeFile(appendForm, md, "utf8");
            written++;
          }
        }
        logger.info(
          `md-mirror: wrote ${written} markdown file${written === 1 ? "" : "s"} ` +
            `from ${htmlFiles.length} HTML page${htmlFiles.length === 1 ? "" : "s"} ` +
            `(${skipped} skipped).`,
        );
      },
    },
  };
}

/** Recursively walk `dir` and yield every `.html` file. */
async function listHtmlFiles(dir: string): Promise<string[]> {
  const out: string[] = [];
  async function walk(d: string) {
    const entries = await readdir(d, { withFileTypes: true });
    for (const e of entries) {
      const p = join(d, e.name);
      if (e.isDirectory()) {
        await walk(p);
      } else if (e.isFile() && e.name.endsWith(".html")) {
        out.push(p);
      }
    }
  }
  await walk(dir);
  return out;
}

/**
 * Convert `dist/`-relative HTML file path to URL slug.
 *   `index.html`                       -> ``           (root)
 *   `getting-started/install/index.html` -> `getting-started/install`
 *   `foo.html`                         -> `foo`
 */
function htmlPathToSlug(rel: string): string {
  if (rel === "index.html") return "";
  if (rel.endsWith("/index.html")) return rel.slice(0, -"/index.html".length);
  if (rel.endsWith(".html")) return rel.slice(0, -".html".length);
  return rel;
}

/**
 * Extract the most-content-bearing region of the HTML. Prefer `<main>`
 * (every Starlight layout and our MarketingLayout uses one); fall back
 * to `<article>`; return null if neither is present.
 */
function extractMain(html: string): string | null {
  const main = matchTag(html, "main");
  if (main) return main;
  const article = matchTag(html, "article");
  if (article) return article;
  return null;
}

/** Naive but resilient: longest non-greedy match of `<tag ...>...</tag>`. */
function matchTag(html: string, tag: string): string | null {
  const re = new RegExp(`<${tag}[\\s>][\\s\\S]*?</${tag}>`, "i");
  const m = html.match(re);
  return m ? m[0] : null;
}

/**
 * Strip every element marked `data-md-skip` (the element and its
 * subtree). Used to hide islands, decorative SVG, theme toggles, and
 * other non-content chrome from the markdown mirror.
 */
function stripSkipMarked(html: string): string {
  // We do this in one pass: find an opening tag carrying data-md-skip,
  // then locate its balanced closing tag accounting for same-name nesting.
  let out = html;
  for (let safety = 0; safety < 200; safety++) {
    const openMatch = out.match(/<([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*\sdata-md-skip\b[^>]*>/);
    if (!openMatch) break;
    const tag = openMatch[1].toLowerCase();
    const openIdx = openMatch.index!;
    const afterOpen = openIdx + openMatch[0].length;
    // Find matching close, honoring nesting of same-tag children.
    const openRe = new RegExp(`<${tag}\\b[^>]*>`, "gi");
    const closeRe = new RegExp(`</${tag}\\s*>`, "gi");
    openRe.lastIndex = afterOpen;
    closeRe.lastIndex = afterOpen;
    let depth = 1;
    let cursor = afterOpen;
    let endIdx = -1;
    while (depth > 0) {
      openRe.lastIndex = cursor;
      closeRe.lastIndex = cursor;
      const nextOpen = openRe.exec(out);
      const nextClose = closeRe.exec(out);
      if (!nextClose) break;
      if (nextOpen && nextOpen.index < nextClose.index) {
        depth++;
        cursor = nextOpen.index + nextOpen[0].length;
      } else {
        depth--;
        if (depth === 0) {
          endIdx = nextClose.index + nextClose[0].length;
          break;
        }
        cursor = nextClose.index + nextClose[0].length;
      }
    }
    if (endIdx < 0) break; // malformed HTML; bail to avoid infinite loop
    out = out.slice(0, openIdx) + out.slice(endIdx);
  }
  return out;
}
