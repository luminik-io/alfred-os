// Onboarding-stepper visual + invariant sweep. Walks every step of the first-run
// stepper at verification widths in dark and light themes, screenshots each,
// and runs the same hard layout invariants the pixel-sweep enforces.
//
//   node scripts/onboarding-sweep.mjs
//   SWEEP_BASE=http://localhost:5294 node scripts/onboarding-sweep.mjs
import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = join(__dirname, "..", ".onboarding-sweep");
const BASE = process.env.SWEEP_BASE || "http://localhost:5294";
const THEME_NAME_STORAGE_KEY = "alfred-theme-name";
const THEME_MODE_STORAGE_KEY = "alfred-theme";

const WIDTHS = [375, 390, 768, 1024, 1280, 1680];
const THEMES = ["dark", "light"];
const HEIGHT = 900;
const STEPS = ["welcome", "engine", "github", "repos", "slack", "request"];
const RAIL_LABELS = {
  welcome: "Welcome",
  engine: "Tools",
  github: "GitHub",
  repos: "Repositories",
  slack: "Slack",
  request: "First request",
};

const PROBE = () => {
  const out = [];
  const srOnly = (el) => el.closest(".sr-only, .visually-hidden, [data-sr-only]") !== null;
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  const label = (el) => {
    const cls =
      typeof el.className === "string"
        ? el.className.split(/\s+/).filter(Boolean).slice(0, 3).join(".")
        : "";
    const aria = el.getAttribute("aria-label");
    return `${el.tagName.toLowerCase()}${cls ? "." + cls : ""}${aria ? `[aria-label="${aria}"]` : ""}`;
  };

  const docOverflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
  if (docOverflow > 1) out.push({ kind: "doc-hscroll", detail: `+${docOverflow}px` });
  const bodyOverflow = document.body.scrollWidth - window.innerWidth;
  if (bodyOverflow > 1) out.push({ kind: "body-hscroll", detail: `+${bodyOverflow}px` });

  const chromeSel = [
    ".alfred-onboarding-shell",
    ".alfred-stepper",
    ".alfred-stepper__track",
    ".alfred-step",
    "[data-slot='card']",
    "header",
    "footer",
  ];
  for (const sel of chromeSel) {
    for (const el of document.querySelectorAll(sel)) {
      if (!vis(el)) continue;
      const s = getComputedStyle(el);
      const scrolls = /(auto|scroll)/.test(s.overflowX);
      const over = el.scrollWidth - el.clientWidth;
      if (over > 1 && !scrolls) {
        out.push({ kind: "chrome-overflow", detail: `${label(el)} +${over}px overflow-x:${s.overflowX}` });
      }
    }
  }

  const textSel = "h1,h2,h3,h4,strong,span,p,small,a,button,label,li";
  for (const el of document.querySelectorAll(textSel)) {
    if (!vis(el) || srOnly(el)) continue;
    const txt = (el.textContent || "").trim();
    if (!txt) continue;
    if (el.querySelector("h1,h2,h3,h4,p,div,ul,ol,section,article")) continue;
    const s = getComputedStyle(el);
    let lh = parseFloat(s.lineHeight);
    if (Number.isNaN(lh)) lh = parseFloat(s.fontSize) * 1.2;
    const r = el.getBoundingClientRect();
    const clipsY =
      /(hidden|clip)/.test(s.overflowY) ||
      s.webkitLineClamp !== "none" ||
      s.display === "-webkit-box";
    if (clipsY && r.height > 0 && r.height < lh - 1.5) {
      out.push({ kind: "subline-clip", detail: `${label(el)} ${r.height.toFixed(0)}<${lh.toFixed(0)} "${txt.slice(0, 30)}"` });
    }
  }

  if (window.innerWidth <= 390) {
    const tapSel = "button, a[href], [role='button'], input[type='checkbox'], input[type='radio']";
    for (const el of document.querySelectorAll(tapSel)) {
      if (!vis(el) || el.closest("[aria-hidden='true']")) continue;
      const r = el.getBoundingClientRect();
      const min = Math.min(r.width, r.height);
      if (min > 0 && min < 36) {
        const t = (el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 24);
        out.push({ kind: "small-tap", detail: `${label(el)} ${r.width.toFixed(0)}x${r.height.toFixed(0)} "${t}"` });
      }
    }
  }
  return out;
};

async function applyTheme(page, theme) {
  await page.evaluate(
    ({ t, nameKey, modeKey }) => {
      const root = document.documentElement;
      root.classList.toggle("dark", t === "dark");
      root.classList.toggle("light", t !== "dark");
      root.setAttribute("data-theme", "alfred");
      try {
        localStorage.setItem(nameKey, "alfred");
        localStorage.setItem(modeKey, t);
      } catch {}
    },
    { t: theme, nameKey: THEME_NAME_STORAGE_KEY, modeKey: THEME_MODE_STORAGE_KEY },
  );
}

async function gotoStep(page, label) {
  const re = new RegExp(`^${label}$`, "i");
  const btn = page.getByRole("button", { name: re }).first();
  await btn.click({ timeout: 2000 });
  await page.waitForTimeout(250);
}

async function run() {
  await mkdir(OUT_DIR, { recursive: true });
  const browser = await chromium.launch();
  const results = [];
  let total = 0;

  for (const theme of THEMES) {
    for (const width of WIDTHS) {
      const context = await browser.newContext({
        viewport: { width, height: HEIGHT },
        deviceScaleFactor: 1,
        colorScheme: theme === "dark" ? "dark" : "light",
      });
      const page = await context.newPage();
      await page.addInitScript(
        ({ t, nameKey, modeKey }) => {
          try {
            localStorage.setItem(nameKey, "alfred");
            localStorage.setItem(modeKey, t);
          } catch {}
        },
        { t: theme, nameKey: THEME_NAME_STORAGE_KEY, modeKey: THEME_MODE_STORAGE_KEY },
      );
      await page.goto(`${BASE}/?tab=settings`, { waitUntil: "domcontentloaded", timeout: 30000 });
      await applyTheme(page, theme);
      await page.waitForTimeout(600);
      await page.waitForSelector(".alfred-onboarding-shell", { timeout: 5000 });

      for (const step of STEPS) {
        await gotoStep(page, RAIL_LABELS[step]);
        const violations = await page.evaluate(PROBE);
        const shot = `${step}_${width}_${theme}.png`;
        try {
          await page.screenshot({ path: join(OUT_DIR, shot), fullPage: false, timeout: 8000 });
        } catch {}
        total += violations.length;
        results.push({ step, width, theme, violations });
      }
      await context.close();
    }
  }
  await browser.close();

  const byKind = {};
  for (const r of results) {
    for (const v of r.violations) {
      (byKind[v.kind] = byKind[v.kind] || []).push(`[${r.step} ${r.width} ${r.theme}] ${v.detail}`);
    }
  }
  console.log(`\n=== ONBOARDING SWEEP: ${results.length} step renders, ${total} violations ===\n`);
  const kinds = Object.keys(byKind).sort();
  if (!kinds.length) {
    console.log("CLEAN: no violations across all steps / widths / themes.");
  } else {
    for (const k of kinds) {
      console.log(`## ${k} (${byKind[k].length})`);
      for (const line of byKind[k]) console.log("  - " + line);
    }
  }
  console.log(`\nScreenshots: ${OUT_DIR}`);
  process.exit(total > 0 ? 1 : 0);
}

run().catch((e) => {
  console.error("SWEEP ERROR:", e);
  process.exit(1);
});
