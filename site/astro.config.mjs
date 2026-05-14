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
