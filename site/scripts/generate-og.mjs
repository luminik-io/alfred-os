import { execFile as execFileCallback } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { promisify } from "node:util";
import sharp from "sharp";

const execFile = promisify(execFileCallback);

const root = resolve(import.meta.dirname, "..");
const logoPath = resolve(root, "src/assets/alfred-logo-transparent.png");
const outPath = resolve(root, "public/brand/alfred-og.png");
const fontDir = resolve(root, "node_modules/@fontsource/quicksand/files");
const fontWeights = [400, 500, 600, 700];

async function fontFace(weight) {
  const fontPath = resolve(fontDir, `quicksand-latin-${weight}-normal.woff2`);
  const font = await readFile(fontPath);
  return `@font-face{font-family:Quicksand;font-style:normal;font-weight:${weight};font-display:block;src:url(data:font/woff2;base64,${font.toString("base64")}) format("woff2");}`;
}

async function imageData(path) {
  const image = await readFile(path);
  return `data:image/png;base64,${image.toString("base64")}`;
}

async function findChrome() {
  const candidates = [
    process.env.CHROME_PATH,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);

  for (const candidate of candidates) {
    try {
      await execFile(candidate, ["--version"]);
      return candidate;
    } catch {
      // Keep looking.
    }
  }

  return null;
}

function html({ fontCss, logoData }) {
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <style>
    ${fontCss}
    * { box-sizing: border-box; }
    body {
      width: 1200px;
      height: 630px;
      margin: 0;
      overflow: hidden;
      background: #050913;
      color: #f8fbff;
      font-family: Quicksand, Arial, sans-serif;
    }
    .canvas {
      position: relative;
      width: 1200px;
      height: 630px;
      padding: 54px 68px;
      background:
        radial-gradient(circle at 16% 17%, rgba(42, 110, 255, 0.36), transparent 38%),
        radial-gradient(circle at 92% 88%, rgba(244, 180, 62, 0.22), transparent 30%),
        linear-gradient(180deg, #071025 0%, #050913 100%);
    }
    .frame {
      position: relative;
      width: 1064px;
      height: 522px;
      padding: 50px 62px;
      overflow: hidden;
      border: 1px solid rgba(87, 137, 255, 0.72);
      border-right-color: rgba(244, 180, 62, 0.46);
      border-radius: 34px;
      background:
        linear-gradient(90deg, rgba(9, 20, 43, 0.92), rgba(7, 12, 26, 0.96)),
        linear-gradient(135deg, rgba(31, 105, 255, 0.10), transparent 55%);
      box-shadow: 0 28px 80px rgba(0, 0, 0, 0.32);
    }
    .frame::before {
      content: "";
      position: absolute;
      inset: 0;
      opacity: 0.32;
      background-image:
        linear-gradient(rgba(94, 137, 239, 0.18) 1px, transparent 1px),
        linear-gradient(90deg, rgba(94, 137, 239, 0.18) 1px, transparent 1px);
      background-size: 150px 108px;
    }
    .mark-bg {
      position: absolute;
      right: 40px;
      top: 126px;
      width: 390px;
      height: 390px;
      opacity: 0.20;
      object-fit: contain;
    }
    .content {
      position: relative;
      z-index: 1;
      height: 100%;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 24px;
    }
    .logo-tile {
      display: grid;
      place-items: center;
      width: 80px;
      height: 80px;
      border: 1px solid #283A62;
      border-radius: 20px;
      background: #07111F;
    }
    .logo-tile img {
      width: 64px;
      height: 64px;
      object-fit: contain;
    }
    h1 {
      margin: 0;
      font-size: 60px;
      line-height: 0.92;
      font-weight: 700;
      letter-spacing: 0;
    }
    .tagline {
      margin: 11px 0 0;
      color: #C7D4F5;
      font-size: 22px;
      line-height: 1.2;
      font-weight: 600;
    }
    .hero {
      max-width: 720px;
    }
    .hero h2 {
      margin: 0;
      max-width: 760px;
      color: #F8FBFF;
      font-size: 54px;
      line-height: 1.08;
      font-weight: 700;
      letter-spacing: 0;
    }
    .hero p {
      margin: 20px 0 0;
      max-width: 740px;
      color: #C7D4F5;
      font-size: 24px;
      line-height: 1.35;
      font-weight: 600;
    }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
    }
    .chip {
      padding: 12px 22px;
      border: 1px solid #314573;
      border-radius: 999px;
      color: #CED9F6;
      background: #101B32;
      font-size: 19px;
      font-weight: 700;
      text-align: center;
    }
    .url,
    .meta {
      color: #91A8DD;
      font-size: 18px;
      font-weight: 700;
    }
    .footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 24px;
    }
  </style>
</head>
<body>
  <main class="canvas">
    <section class="frame">
      <img class="mark-bg" src="${logoData}" alt="" />
      <div class="content">
        <div class="brand">
          <div class="logo-tile"><img src="${logoData}" alt="" /></div>
          <div>
            <h1>Alfred</h1>
            <p class="tagline">Local AI agents for real software work.</p>
          </div>
        </div>
        <div class="hero">
          <h2>Local agent fleet for Claude Code + Codex.</h2>
          <p>GitHub issues in. PRs and reviews out.</p>
        </div>
        <div class="chips">
          <div class="chip">multi-repo</div>
          <div class="chip">clean worktrees</div>
          <div class="chip">PRs + Slack reports</div>
        </div>
        <div class="footer">
          <div class="url">alfred.luminik.io</div>
          <div class="meta">MIT · Python · macOS/Linux</div>
        </div>
      </div>
    </section>
  </main>
</body>
</html>`;
}

async function renderWithChrome(htmlText) {
  const chrome = await findChrome();
  if (!chrome) return false;

  const tempDir = await mkdtemp(resolve(tmpdir(), "alfred-og-"));
  const htmlPath = resolve(tempDir, "index.html");
  const screenshotPath = resolve(tempDir, "alfred-og.png");

  try {
    await writeFile(htmlPath, htmlText);
    await execFile(chrome, [
      "--headless=new",
      "--disable-gpu",
      "--hide-scrollbars",
      "--no-first-run",
      "--no-default-browser-check",
      "--virtual-time-budget=1000",
      "--window-size=1200,630",
      `--screenshot=${screenshotPath}`,
      `file://${htmlPath}`,
    ]);

    await mkdir(dirname(outPath), { recursive: true });
    await sharp(screenshotPath).resize(1200, 630).png().toFile(outPath);
    return true;
  } finally {
    await rm(tempDir, { recursive: true, force: true });
  }
}

async function renderWithSharp(htmlText) {
  const escaped = htmlText
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  const fallbackSvg = `<?xml version="1.0" encoding="UTF-8"?>
<svg width="1200" height="630" viewBox="0 0 1200 630" xmlns="http://www.w3.org/2000/svg">
  <rect width="1200" height="630" fill="#050913"/>
  <foreignObject width="1200" height="630">
    ${escaped}
  </foreignObject>
</svg>`;

  await mkdir(dirname(outPath), { recursive: true });
  await sharp(Buffer.from(fallbackSvg)).png().toFile(outPath);
}

const fontCss = (await Promise.all(fontWeights.map(fontFace))).join("");
const logoData = await imageData(logoPath);
const htmlText = html({ fontCss, logoData });
const rendered = await renderWithChrome(htmlText);

if (!rendered) {
  await renderWithSharp(htmlText);
}

console.log(`wrote ${outPath}`);
