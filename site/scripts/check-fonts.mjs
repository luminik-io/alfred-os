import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");

const requiredDependencies = [
  "@fontsource-variable/instrument-sans",
  "@fontsource-variable/quicksand",
  "@fontsource/fragment-mono",
];
const bannedDependencies = [
  "@fontsource-variable/space-grotesk",
  "@fontsource/inter",
  "@fontsource/jetbrains-mono",
  "@fontsource/quicksand",
];
const runtimeFiles = [
  "../lib/server/static/style.css",
  "src/layouts/MarketingLayout.astro",
  "src/styles/marketing.css",
  "src/styles/custom.css",
  "scripts/generate-og.mjs",
  "src/components/marketing/BatmanFanOut.astro",
  "src/components/marketing/SpecsDrivenFlow.astro",
  "src/components/marketing/FiringLifecycle.astro",
];
const bannedRuntimePatterns = [
  {
    name: "Space Grotesk",
    pattern: /Space Grotesk|@fontsource-variable\/space-grotesk/,
  },
  {
    name: "Inter",
    pattern: /\bInter\b|@fontsource\/inter/,
  },
  {
    name: "JetBrains Mono",
    pattern: /JetBrains Mono|@fontsource\/jetbrains-mono/,
  },
];

const packageJson = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"));
const dependencies = {
  ...(packageJson.dependencies ?? {}),
  ...(packageJson.devDependencies ?? {}),
};
const failures = [];

for (const dependency of requiredDependencies) {
  if (!dependencies[dependency]) {
    failures.push(`missing required dependency: ${dependency}`);
  }
}

for (const dependency of bannedDependencies) {
  if (dependencies[dependency]) {
    failures.push(`remove stale font dependency: ${dependency}`);
  }
}

for (const relative of runtimeFiles) {
  const text = readFileSync(resolve(root, relative), "utf8");
  const staleFonts = bannedRuntimePatterns
    .filter(({ pattern }) => pattern.test(text))
    .map(({ name }) => name);

  if (staleFonts.length) {
    failures.push(`${relative} references retired font(s): ${staleFonts.join(", ")}`);
  }
}

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
