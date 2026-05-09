// @ts-check
import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";

// Alfred-OS site config.
//
// Default URL assumes deployment to luminik-io.github.io/alfred-os (project
// pages). When a dedicated org and CNAME exist (alfred-os.dev or similar),
// override `site` and clear `base`.
export default defineConfig({
  site: process.env.ALFRED_OS_SITE_URL ?? "https://luminik-io.github.io",
  base: process.env.ALFRED_OS_SITE_BASE ?? "/alfred-os",
  trailingSlash: "ignore",
  integrations: [
    starlight({
      title: "Alfred-OS",
      description:
        "Launchd-managed Claude Code agent fleet for solo founders. " +
        "One Mac, one operator, code shipping while you sleep.",
      // logo: {
      //   src: './src/assets/logo.svg',  // operator: drop a real logo here
      // },
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
            content:
              "https://opengraph.githubassets.com/1/luminik-io/alfred-os",
          },
        },
        {
          tag: "meta",
          attrs: {
            name: "twitter:card",
            content: "summary_large_image",
          },
        },
      ],
      sidebar: [
        {
          label: "Getting started",
          items: [
            { label: "What is alfred-os?", slug: "" },
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
