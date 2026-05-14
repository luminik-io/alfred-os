// @ts-check
import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import mermaid from "astro-mermaid";

// Alfred site config.
//
// Default URL assumes the public custom domain. Forks can override these with
// ALFRED_OS_SITE_URL / ALFRED_OS_SITE_BASE when deploying under a project path.
const SITE_URL = process.env.ALFRED_OS_SITE_URL ?? "https://alfred.luminik.io";

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
        "Documentation for Alfred OS, the open-source runtime for a fleet of " +
        "autonomous Claude Code agents on a single machine you own.",
    },
    {
      "@type": "SoftwareApplication",
      name: "Alfred",
      alternateName: "Alfred OS",
      applicationCategory: "DeveloperApplication",
      operatingSystem: "macOS, Linux",
      url: SITE_URL,
      description:
        "A local agent-fleet runtime for solo builders. Claude Code agents " +
        "scheduled by launchd or systemd, each firing isolated in its own git " +
        "worktree, with per-agent IAM and per-day spend caps.",
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
    clone.removeAttribute("id");
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
    hint.textContent = "scroll to zoom \\u00b7 drag to pan \\u00b7 esc to close";
    overlay.appendChild(stage);
    overlay.appendChild(hint);
    document.body.appendChild(overlay);
    let scale = 1, tx = 0, ty = 0, dragging = false, sx = 0, sy = 0;
    const apply = () => {
      stage.style.transform = "translate(" + tx + "px," + ty + "px) scale(" + scale + ")";
    };
    overlay.addEventListener("wheel", (e) => {
      e.preventDefault();
      scale = Math.min(8, Math.max(0.5, scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
      apply();
    }, { passive: false });
    stage.addEventListener("pointerdown", (e) => {
      dragging = true; sx = e.clientX - tx; sy = e.clientY - ty;
      stage.setPointerCapture(e.pointerId);
    });
    stage.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      tx = e.clientX - sx; ty = e.clientY - sy; apply();
    });
    stage.addEventListener("pointerup", () => { dragging = false; });
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
  base: process.env.ALFRED_OS_SITE_BASE ?? "/",
  trailingSlash: "ignore",
  integrations: [
    // astro-mermaid must run before starlight: it rewrites ```mermaid fenced
    // blocks into a client-rendered <pre class="mermaid"> before Starlight's
    // markdown pipeline turns them into static code blocks. autoTheme keeps
    // diagrams in sync with Starlight's light/dark toggle.
    mermaid({
      theme: "default",
      autoTheme: true,
      // astro-mermaid v2 nests raw mermaid options under mermaidConfig.
      mermaidConfig: {
        themeVariables: {
          primaryColor: "#141d33",
          lineColor: "#4a78ff",
          textColor: "#dfe5f2",
          primaryBorderColor: "#2a3450",
        },
      },
    }),
    starlight({
      title: "Alfred",
      description:
        "Launchd-managed Claude Code agent fleet for solo founders. " +
        "One Mac, one operator, code shipping while you sleep.",
      logo: {
        src: "./src/assets/alfred-logo.png",
        alt: "Alfred logo",
      },
      favicon: "/favicon.png",
      social: [
        { icon: "github", label: "GitHub", href: "https://github.com/luminik-io/alfred-os" },
      ],
      editLink: {
        baseUrl:
          "https://github.com/luminik-io/alfred-os/edit/main/site/",
      },
      lastUpdated: true,
      tableOfContents: { minHeadingLevel: 2, maxHeadingLevel: 4 },
      customCss: ["./src/styles/custom.css"],
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
            property: "og:image",
            content: "https://alfred.luminik.io/brand/alfred-og.png",
          },
        },
        {
          tag: "meta",
          attrs: {
            name: "twitter:image",
            content: "https://alfred.luminik.io/brand/alfred-og.png",
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
            href: "/apple-touch-icon.png",
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
      ],
      sidebar: [
        {
          label: "Getting started",
          items: [
            { label: "What is Alfred?", slug: "" },
            { label: "Install", slug: "getting-started/install" },
            { label: "Your first agent", slug: "getting-started/tutorial" },
            { label: "Dry-run mode", slug: "getting-started/dry-run" },
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
            { label: "Severity routing", slug: "concepts/severity-routing" },
          ],
        },
        {
          label: "Guides",
          items: [
            { label: "Claude Code", slug: "guides/claude-code" },
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
            { label: "launchd plist template", slug: "reference/launchd" },
            { label: "Environment variables", slug: "reference/env" },
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
  ],
});
