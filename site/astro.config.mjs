// @ts-check
import { defineConfig } from "astro/config";
import sitemap from "@astrojs/sitemap";
import starlight from "@astrojs/starlight";
import mermaid from "astro-mermaid";
import { mdMirror } from "./src/integrations/md-mirror.ts";

// Google Analytics 4 property. Hardcoded fallback so the build emits the
// snippet without per-deploy env-var setup; PUBLIC_ALFRED_GA4_ID still
// overrides for forks or staging deploys that want their own property.
const GA4_ID = process.env.PUBLIC_ALFRED_GA4_ID ?? "G-Y157X0YLN4";

// Cookie-consent banner. Self-contained vanilla-JS bootstrap that runs on
// every page (docs + marketing) and short-circuits if the visitor has
// already decided. Accept flips Google Consent Mode v2 analytics_storage
// from default-denied to granted so the gtag config above starts
// measuring. Reject / dismiss leaves consent denied and stores the
// choice so the banner doesn't re-appear. Storage key
// "alfred-cookie-consent" matches the conditional gtag call above.
const COOKIE_BANNER_SCRIPT = `
(function(){
  if (typeof document === "undefined" || typeof localStorage === "undefined") return;
  function init(){
    try {
      if (localStorage.getItem("alfred-cookie-consent")) return;
    } catch (e) { return; }
    if (document.getElementById("alfred-cookie-banner")) return;
    var b = document.createElement("div");
    b.id = "alfred-cookie-banner";
    b.className = "alfred-cookie-banner";
    b.setAttribute("role", "dialog");
    b.setAttribute("aria-label", "Cookie consent");
    b.innerHTML =
      '<p class="alfred-cookie-text">' +
        'We use Google Analytics to understand how the docs and marketing pages are used. ' +
        'No ads, no profiling, IP anonymised. Read our ' +
        '<a href="https://policies.google.com/technologies/partner-sites" rel="noopener">data note</a>.' +
      '</p>' +
      '<div class="alfred-cookie-actions">' +
        '<button type="button" class="alfred-cookie-btn alfred-cookie-deny" data-choice="deny">Reject</button>' +
        '<button type="button" class="alfred-cookie-btn alfred-cookie-allow" data-choice="allow">Accept</button>' +
      '</div>';
    document.body.appendChild(b);
    function pick(choice){
      try { localStorage.setItem("alfred-cookie-consent", choice); } catch (e) {}
      if (choice === "allow" && typeof window.gtag === "function") {
        window.gtag("consent", "update", { analytics_storage: "granted" });
      }
      b.remove();
    }
    b.addEventListener("click", function(ev){
      var t = ev.target;
      if (t && t.dataset && t.dataset.choice) pick(t.dataset.choice);
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
`;

// Alfred site config.
//
// Default URL assumes the public custom domain. Forks can override these with
// ALFRED_OS_SITE_URL / ALFRED_OS_SITE_BASE when deploying under a project path.
const SITE_URL = process.env.ALFRED_OS_SITE_URL ?? "https://alfred.luminik.io";
const SITE_BASE = process.env.ALFRED_OS_SITE_BASE ?? "/";
/** @param {string} path */
const withBase = (path) =>
  `${SITE_BASE.replace(/\/$/, "")}/${path.replace(/^\//, "")}`.replace(/\/{2,}/g, "/");
/** @param {string} path */
const siteAssetUrl = (path) => new URL(withBase(path), SITE_URL).href;

// JSON-LD structured data, injected on every page. WebSite + SoftwareApplication
// describe the project itself (not the individual page), so a site-wide graph
// is correct. Helps search engines and AI crawlers classify Alfred as a free,
// open-source developer tool rather than guessing from prose.
const STRUCTURED_DATA = JSON.stringify({
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "WebSite",
      name: "Alfred",
      url: SITE_URL,
      description:
        "Documentation for Alfred, the open-source runtime for autonomous engineering " +
        "agents on Claude Code and Codex.",
    },
    {
      "@type": "SoftwareApplication",
      name: "Alfred",
      applicationCategory: "DeveloperApplication",
      operatingSystem: "macOS, Linux",
      url: SITE_URL,
      description:
        "A local runtime and coordination layer that turns Slack requests, GitHub issues, specs, and PR " +
        "feedback into autonomous Claude Code or Codex runs with isolated git " +
        "worktrees, label state, reviews, tests, and Slack reports.",
      downloadUrl: "https://github.com/luminik-io/alfred-os",
      softwareHelp: SITE_URL,
      license: "https://github.com/luminik-io/alfred-os/blob/main/LICENSE",
      isAccessibleForFree: true,
      offers: { "@type": "Offer", price: "0", priceCurrency: "USD" },
      author: { "@type": "Organization", name: "DataRavel Inc." },
    },
  ],
});

// Click-to-zoom for rendered mermaid diagrams. astro-mermaid has no zoom of
// its own, and the dense flowcharts and sequence diagrams are hard to read
// inline (especially on mobile). This is a small, dependency-free behaviour:
// a delegated click on a rendered `pre.mermaid` opens the SVG in a full-screen
// overlay with wheel-zoom and drag-pan; Escape or a backdrop click closes it.
// The matching styles (.mermaid-zoom-*) live in src/styles/custom.css.
const MERMAID_ZOOM_SCRIPT = `
(() => {
  if (typeof document === "undefined") return;
  const OVERLAY_ID = "mermaid-zoom-overlay";
  const onKey = (e) => { if (e.key === "Escape") close(); };
  function close() {
    const o = document.getElementById(OVERLAY_ID);
    if (o) o.remove();
    document.removeEventListener("keydown", onKey);
  }
  function open(svg) {
    close();
    const overlay = document.createElement("div");
    overlay.id = OVERLAY_ID;
    overlay.className = "mermaid-zoom-overlay";
    const stage = document.createElement("div");
    stage.className = "mermaid-zoom-stage";
    const clone = svg.cloneNode(true);
    // Mermaid scopes its themed <style> rules under the SVG's id
    // (#mermaid-xxx .node rect { fill: ... }). Dropping the id silently
    // unstyles the clone to raw black-on-white SVG defaults. Instead, give
    // the clone a fresh unique id and rewrite the scoped selectors in its
    // <style> block to match, so the theme colors survive the clone.
    const oldId = clone.getAttribute("id");
    const newId = "mermaid-zoom-svg-" + Math.random().toString(36).slice(2, 9);
    clone.setAttribute("id", newId);
    if (oldId) {
      clone.querySelectorAll("style").forEach((s) => {
        s.textContent = s.textContent.split("#" + oldId).join("#" + newId);
      });
    }
    clone.removeAttribute("style");
    // Mermaid renders the SVG as width="100%" sized by an inline max-width
    // style. Stripped of that style, and with no sized container in the
    // overlay, width="100%" collapses the clone to a few pixels. Give it
    // explicit pixel dimensions from its viewBox, scaled to fit the
    // viewport while keeping aspect ratio.
    const vb = (clone.getAttribute("viewBox") || "").split(/[\\s,]+/).map(Number);
    let baseW, baseH;
    if (vb.length === 4 && vb[2] > 0 && vb[3] > 0) {
      const fit = Math.min(
        (window.innerWidth * 0.86) / vb[2],
        (window.innerHeight * 0.78) / vb[3]
      );
      baseW = Math.round(vb[2] * fit);
      baseH = Math.round(vb[3] * fit);
    } else {
      const r = svg.getBoundingClientRect();
      baseW = Math.round(r.width) || 320;
      baseH = Math.round(r.height) || 200;
    }
    clone.setAttribute("width", String(baseW));
    clone.setAttribute("height", String(baseH));
    stage.appendChild(clone);
    const hint = document.createElement("div");
    hint.className = "mermaid-zoom-hint";
    hint.textContent = "scroll or pinch to zoom \\u00b7 drag to pan \\u00b7 esc to close";
    overlay.appendChild(stage);
    overlay.appendChild(hint);
    document.body.appendChild(overlay);
    let scale = 1, tx = 0, ty = 0, dragging = false, sx = 0, sy = 0;
    const clamp = (s) => Math.min(8, Math.max(0.5, s));
    const apply = () => {
      stage.style.transform = "translate(" + tx + "px," + ty + "px) scale(" + scale + ")";
    };
    overlay.addEventListener("wheel", (e) => {
      e.preventDefault();
      scale = clamp(scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15));
      apply();
    }, { passive: false });
    // Pointer events cover both mouse drag-pan and touch. Touch pinch-zoom
    // needs two tracked pointers: with one down we pan, with two down we
    // scale by the ratio of their current to starting separation. CSS sets
    // touch-action: none on the stage, so the browser hands us every touch
    // instead of running its own pan/zoom.
    const pointers = new Map();
    let pinchStartDist = 0, pinchStartScale = 1;
    const dist2 = () => {
      const p = [...pointers.values()];
      return Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
    };
    stage.addEventListener("pointerdown", (e) => {
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      // setPointerCapture can throw if the pointer is no longer active;
      // it is a nice-to-have for drag tracking, not worth aborting on.
      try { stage.setPointerCapture(e.pointerId); } catch (_) {}
      if (pointers.size === 1) {
        dragging = true; sx = e.clientX - tx; sy = e.clientY - ty;
      } else if (pointers.size === 2) {
        dragging = false;
        pinchStartDist = dist2();
        pinchStartScale = scale;
      }
    });
    stage.addEventListener("pointermove", (e) => {
      if (!pointers.has(e.pointerId)) return;
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.size >= 2) {
        if (pinchStartDist > 0) {
          scale = clamp(pinchStartScale * (dist2() / pinchStartDist));
          apply();
        }
      } else if (dragging) {
        tx = e.clientX - sx; ty = e.clientY - sy; apply();
      }
    });
    const release = (e) => {
      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinchStartDist = 0;
      if (pointers.size === 1) {
        // One finger left after a pinch: resume panning from it.
        const p = [...pointers.values()][0];
        dragging = true; sx = p.x - tx; sy = p.y - ty;
      } else if (pointers.size === 0) {
        dragging = false;
      }
    };
    stage.addEventListener("pointerup", release);
    stage.addEventListener("pointercancel", release);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    document.addEventListener("keydown", onKey);
  }
  document.addEventListener("click", (e) => {
    if (document.getElementById(OVERLAY_ID)) return;
    const pre = e.target.closest && e.target.closest("pre.mermaid");
    if (!pre) return;
    const svg = pre.querySelector("svg");
    if (!svg) return;
    if (window.getSelection && String(window.getSelection())) return;
    open(svg);
  });
})();
`;

export default defineConfig({
  site: SITE_URL,
  base: SITE_BASE,
  trailingSlash: "ignore",
  integrations: [
    // @astrojs/sitemap emits /sitemap-index.xml + /sitemap-0.xml at build
    // time from every page Astro renders. Hooks straight into the site
    // URL above so canonical links match. Search engines + AI crawlers
    // both consume sitemap-index.xml directly; the robots.txt under
    // public/ points at it explicitly so there's no autodiscovery
    // dependence on the hostname being on the well-known list.
    sitemap(),
    // astro-mermaid must run before starlight: it rewrites ```mermaid fenced
    // blocks into a client-rendered <pre class="mermaid"> before Starlight's
    // markdown pipeline turns them into static code blocks. autoTheme keeps
    // diagrams in sync with Starlight's light/dark toggle.
    mermaid({
      theme: "default",
      autoTheme: true,
      enableLog: false,
      // astro-mermaid swaps only the `theme` name (default/dark) on the
      // light/dark toggle; any themeVariables here apply to BOTH themes.
      // Keep only theme-neutral values: lineColor reads fine on light and
      // dark backgrounds alike. Text and node-fill colors are left to
      // mermaid's stock default/dark themes so diagram text stays legible
      // in light mode (dark-tuned overrides made it near-invisible).
      mermaidConfig: {
        themeVariables: {
          lineColor: "#4a78ff",
        },
      },
    }),
    starlight({
      title: "Alfred",
      description:
        "Run Claude Code and Codex as autonomous engineering agents. " +
        "GitHub issues, specs, worktrees, PRs, reviews, tests, and Slack reports.",
      logo: {
        src: "./src/assets/alfred-logo-transparent.png",
        alt: "Alfred logo",
      },
      favicon: withBase("/favicon.png"),
      social: [
        { icon: "github", label: "GitHub", href: "https://github.com/luminik-io/alfred-os" },
      ],
      editLink: {
        baseUrl:
          "https://github.com/luminik-io/alfred-os/edit/main/site/",
      },
      lastUpdated: true,
      // Use the custom marketing 404 at src/pages/404.astro instead of
      // Starlight's docs-shell default (which read as a broken docs page).
      disable404Route: true,
      tableOfContents: { minHeadingLevel: 2, maxHeadingLevel: 4 },
      customCss: ["./src/styles/custom.css", "./src/styles/cookie-banner.css"],
      components: {
        // Append a DataRavel Inc. copyright line under the default page footer.
        Footer: "./src/components/Footer.astro",
      },
      head: [
        {
          tag: "meta",
          attrs: { name: "theme-color", content: "#0d1322" },
        },
        {
          tag: "meta",
          attrs: {
            name: "keywords",
            content:
              "autonomous coding agents, Claude Code, Codex, Codex CLI, self-hosted ai agents, engineering agents, GitHub agents, multi-repo, monorepo, agent runtime, Alfred, open source, MIT, specs-driven development",
          },
        },
        {
          tag: "meta",
          attrs: { name: "author", content: "DataRavel Inc." },
        },
        {
          tag: "meta",
          attrs: { name: "robots", content: "index, follow" },
        },
        {
          tag: "meta",
          attrs: { property: "og:site_name", content: "Alfred" },
        },
        {
          tag: "meta",
          attrs: { property: "og:locale", content: "en_US" },
        },
        {
          tag: "meta",
          attrs: { property: "og:type", content: "website" },
        },
        {
          tag: "meta",
          attrs: {
            property: "og:image",
            content: siteAssetUrl("/brand/alfred-og.png"),
          },
        },
        {
          tag: "meta",
          attrs: {
            property: "og:image:width",
            content: "1200",
          },
        },
        {
          tag: "meta",
          attrs: {
            property: "og:image:height",
            content: "630",
          },
        },
        {
          tag: "meta",
          attrs: {
            property: "og:image:type",
            content: "image/png",
          },
        },
        {
          tag: "meta",
          attrs: {
            property: "og:image:alt",
            content: "Alfred. Coding agents that keep development moving while you are away.",
          },
        },
        {
          tag: "meta",
          attrs: {
            name: "twitter:image",
            content: siteAssetUrl("/brand/alfred-og.png"),
          },
        },
        {
          tag: "meta",
          attrs: {
            name: "twitter:image:alt",
            content: "Alfred. Coding agents that keep development moving while you are away.",
          },
        },
        {
          tag: "meta",
          attrs: {
            name: "twitter:card",
            content: "summary_large_image",
          },
        },
        {
          tag: "link",
          attrs: {
            rel: "apple-touch-icon",
            href: withBase("/apple-touch-icon.png"),
          },
        },
        {
          tag: "script",
          attrs: { type: "application/ld+json" },
          content: STRUCTURED_DATA,
        },
        {
          tag: "script",
          content: MERMAID_ZOOM_SCRIPT,
        },
        // Google Analytics 4 + cookie-consent gate.
        //
        // The loader stays in a paused state until the visitor accepts the
        // banner (alfred-cookie-consent=allow in localStorage). Until then,
        // gtag is queued and Google Consent Mode v2 holds analytics_storage,
        // ad_storage, ad_user_data, ad_personalization in "denied". When
        // the operator clicks Accept on the banner we flip the consent to
        // "granted" and the queued events flush. Clicking Reject (or
        // dismissing) keeps consent denied and no measurement events fire.
        // Same pattern is used in src/layouts/MarketingLayout.astro for the
        // marketing pages, so behaviour is uniform across the whole site.
        ...(GA4_ID
          ? /** @type {const} */ ([
              {
                tag: /** @type {"script"} */ ("script"),
                attrs: {
                  async: true,
                  src: `https://www.googletagmanager.com/gtag/js?id=${GA4_ID}`,
                },
              },
              {
                tag: /** @type {"script"} */ ("script"),
                content: `window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}gtag("consent","default",{analytics_storage:"denied",ad_storage:"denied",ad_user_data:"denied",ad_personalization:"denied",wait_for_update:500});gtag("js",new Date());gtag("config","${GA4_ID}",{anonymize_ip:true});try{var c=localStorage.getItem("alfred-cookie-consent");if(c==="allow"){gtag("consent","update",{analytics_storage:"granted"});}}catch(e){}`,
              },
            ])
          : []),
        // Cookie consent banner. Inlines a tiny vanilla-JS bootstrapper
        // that runs on every page (docs + marketing). The banner appears
        // when localStorage has no record of an accept/dismiss decision;
        // Accept flips Google Consent Mode v2 analytics_storage to
        // granted (so the gtag config above starts measuring) and stores
        // the choice. Reject / dismiss writes "deny" and leaves consent
        // in the denied default. The CSS is in src/styles/custom.css
        // (.alfred-cookie-*) so the same design tokens drive light/dark.
        {
          tag: /** @type {"script"} */ ("script"),
          content: COOKIE_BANNER_SCRIPT,
        },
        ...(process.env.PUBLIC_ALFRED_GSC_TOKEN
          ? /** @type {const} */ ([
              {
                tag: /** @type {"meta"} */ ("meta"),
                attrs: {
                  name: "google-site-verification",
                  content: process.env.PUBLIC_ALFRED_GSC_TOKEN,
                },
              },
            ])
          : []),
      ],
      sidebar: [
        {
          label: "Getting started",
          items: [
            { label: "What is Alfred?", slug: "docs" },
            { label: "Install", slug: "getting-started/install" },
            { label: "How long does install take", slug: "getting-started/install-time" },
            { label: "AI-assisted install", slug: "getting-started/ai-assisted-install" },
            { label: "Workspace patterns", slug: "getting-started/workspace-patterns" },
            { label: "Your first agent", slug: "getting-started/tutorial" },
            { label: "Dry-run mode", slug: "getting-started/dry-run" },
            { label: "Operating the fleet", slug: "getting-started/operating-the-fleet" },
          ],
        },
        {
          label: "Concepts",
          items: [
            { label: "Architecture", slug: "concepts/architecture" },
            { label: "How it works", slug: "concepts/how-it-works" },
            { label: "The agent fleet", slug: "concepts/fleet" },
            { label: "Codename pattern", slug: "concepts/codename-pattern" },
            { label: "Issue claim state machine", slug: "concepts/state-machine" },
            { label: "State and memory", slug: "concepts/state-and-memory" },
            { label: "Fleet brain", slug: "concepts/fleet-brain" },
            { label: "Slack-native planning", slug: "concepts/slack-native-planning" },
            { label: "Plain mode", slug: "concepts/plain-mode" },
            { label: "Alfred Desktop", slug: "concepts/native-client" },
            { label: "Desktop app guide", slug: "concepts/desktop-client" },
            { label: "Layered install", slug: "concepts/layered-install" },
            { label: "Disk guardian", slug: "concepts/disk-guardian" },
            { label: "Engine routing", slug: "concepts/engine-routing" },
            { label: "Severity routing", slug: "concepts/severity-routing" },
          ],
        },
        {
          label: "Guides",
          items: [
            { label: "Claude Code and Codex", slug: "guides/claude-code" },
            { label: "Specs-driven development", slug: "guides/specs-driven-development" },
            { label: "Alfred on a monorepo", slug: "guides/monorepo" },
            { label: "Worked example: three repos", slug: "guides/multi-repo-worked-example" },
            { label: "Slack", slug: "guides/slack" },
            { label: "AWS", slug: "guides/aws" },
            { label: "Skills", slug: "guides/skills" },
            { label: "Integrations", slug: "guides/integrations" },
            { label: "Hermes", slug: "guides/hermes" },
            { label: "Linux", slug: "guides/linux" },
          ],
        },
        {
          label: "Reference",
          items: [
            { label: "agent_runner API", slug: "reference/agent-runner" },
            { label: "Operator CLI", slug: "reference/cli" },
            { label: "Telemetry", slug: "reference/telemetry" },
            { label: "launchd plist template", slug: "reference/launchd" },
            { label: "Environment variables", slug: "reference/env" },
            { label: "Output samples", slug: "reference/output-samples" },
            { label: "Glossary", slug: "reference/glossary" },
          ],
        },
        {
          label: "About",
          items: [
            { label: "Roadmap", slug: "about/roadmap" },
            { label: "Contributing", slug: "about/contributing" },
            { label: "Changelog", slug: "about/changelog" },
            { label: "Security", slug: "about/security" },
          ],
        },
      ],
    }),
    // md-mirror runs at astro:build:done (AFTER starlight has produced
    // the static HTML). It walks dist/ and emits a .md sibling for every
    // page so AI crawlers (ChatGPT, Claude, Perplexity) can ingest raw
    // markdown instead of converting our HTML themselves. See
    // src/integrations/md-mirror.ts for opt-out mechanisms.
    mdMirror({ excludePaths: ["404"] }),
  ],
});
