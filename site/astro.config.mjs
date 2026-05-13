// @ts-check
import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";

// Alfred site config.
//
// Default URL assumes the public custom domain. Forks can override these with
// ALFRED_OS_SITE_URL / ALFRED_OS_SITE_BASE when deploying under a project path.
export default defineConfig({
  site: process.env.ALFRED_OS_SITE_URL ?? "https://alfred.luminik.io",
  base: process.env.ALFRED_OS_SITE_BASE ?? "/",
  trailingSlash: "ignore",
  integrations: [
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
      head: [
        {
          tag: "meta",
          attrs: { name: "theme-color", content: "#0d1117" },
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
      ],
      sidebar: [
        {
          label: "Getting started",
          items: [
            { label: "What is Alfred?", slug: "" },
            { label: "Install", slug: "getting-started/install" },
            { label: "Your first agent", slug: "getting-started/tutorial" },
          ],
        },
        {
          label: "Concepts",
          items: [
            { label: "Architecture", slug: "concepts/architecture" },
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
            { label: "Publishing", slug: "guides/publishing" },
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
