import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

// This test guards the operator font directive (2026-06-13) that refactors keep
// silently reverting. It reads the real source of truth (the css file) so a
// revert fails CI even when the human-readable guard comments get stripped.
//
//   Alfred uses Instrument Sans for headings, Quicksand for interface/body copy,
//   and Fragment Mono for code. Space Grotesk / JetBrains are removed as
//   default-AI-product fonts.

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
  it("imports the directed Instrument Sans, Quicksand, and Fragment Mono faces", () => {
    const imports = fontImportLines(readIndexCss()).join("\n").toLowerCase();
    expect(imports).toContain("instrument-sans");
    expect(imports).toContain("quicksand");
    expect(imports).toContain("fragment-mono");
  });

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
