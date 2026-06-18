import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { PRIMARY_TABS } from "../lib/primaryTabs";

// These tests guard two operator directives that Codex refactors keep silently
// reverting. They read the real source of truth (the array and the css file) so
// a revert fails CI even when the human-readable guard comments get stripped.
//
//  1. Alfred uses Instrument Sans for headings, Quicksand for interface/body
//     copy, and Fragment Mono for code. Space Grotesk / JetBrains are removed
//     as default-AI-product fonts.
//  2. The primary nav uses the job-shaped IA labels Inbox / Ask / Work /
//     Agents / Setup, so non-technical users do not have to decode runtime nouns.

const indexCssPath = resolve(__dirname, "..", "index.css");

function readIndexCss(): string {
  return readFileSync(indexCssPath, "utf8");
}

// The lines that actually load a font family, ignoring comments. A comment may
// legitimately name a banned font to explain why it was removed.
function fontImportLines(css: string): string[] {
  return css
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.startsWith("@import") && line.includes("fontsource"));
}

function resolvedFontToken(css: string, token: "--font-heading" | "--font-sans"): string {
  const match = css.match(new RegExp(`${token}:\\s*([^;]+);`));
  if (!match) {
    throw new Error(`could not find ${token} token in index.css`);
  }
  return match[1].trim();
}

describe("operator font directive (do not revert)", () => {
  it("keeps --font-heading on Instrument Sans", () => {
    const heading = resolvedFontToken(readIndexCss(), "--font-heading");
    expect(heading.toLowerCase().startsWith('"instrument sans')).toBe(true);
  });

  it("keeps --font-sans on Quicksand", () => {
    const sans = resolvedFontToken(readIndexCss(), "--font-sans");
    expect(sans.toLowerCase().startsWith('"quicksand')).toBe(true);
  });

  it("does not import Space Grotesk or JetBrains fonts", () => {
    const imports = fontImportLines(readIndexCss()).join("\n").toLowerCase();
    expect(imports).not.toContain("space-grotesk");
    expect(imports).not.toContain("jetbrains");
  });
});

describe("primary nav job-shaped IA (do not revert)", () => {
  it("uses exactly the product labels Inbox / Ask / Work / Agents / Setup", () => {
    expect(PRIMARY_TABS.map((tab) => tab.label)).toEqual([
      "Inbox",
      "Ask",
      "Work",
      "Agents",
      "Setup",
    ]);
  });

  it("does not reintroduce the older runtime-noun labels", () => {
    const labels = PRIMARY_TABS.map((tab) => tab.label);
    for (const banned of ["Home", "Pipeline", "Fleet", "Lessons"]) {
      expect(labels).not.toContain(banned);
    }
  });
});
