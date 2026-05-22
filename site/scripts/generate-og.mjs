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

function card(title, items, dotColor) {
  return `<section class="card">
    <div class="card-title"><span style="background:${dotColor}"></span>${title}</div>
    <ul>${items.map((item) => `<li>${item}</li>`).join("")}</ul>
  </section>`;
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
      padding: 58px 70px;
      background:
        radial-gradient(circle at 17% 16%, rgba(42, 110, 255, 0.32), transparent 38%),
        radial-gradient(circle at 92% 88%, rgba(244, 180, 62, 0.22), transparent 30%),
        linear-gradient(180deg, #071025 0%, #050913 100%);
    }
    .frame {
      position: relative;
      width: 1060px;
      height: 514px;
      padding: 42px;
      border: 2px solid rgba(87, 137, 255, 0.78);
      border-right-color: rgba(244, 180, 62, 0.52);
      border-radius: 34px;
      background:
        linear-gradient(90deg, rgba(9, 20, 43, 0.92), rgba(7, 12, 26, 0.96)),
        linear-gradient(135deg, rgba(31, 105, 255, 0.10), transparent 55%);
      box-shadow: 0 28px 80px rgba(0, 0, 0, 0.32);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 28px;
    }
    .logo-tile {
      display: grid;
      place-items: center;
      width: 86px;
      height: 86px;
      border: 1px solid #283A62;
      border-radius: 22px;
      background: #07111F;
    }
    .logo-tile img {
      width: 70px;
      height: 70px;
      object-fit: contain;
    }
    h1 {
      margin: 0;
      font-size: 64px;
      line-height: 0.92;
      font-weight: 700;
      letter-spacing: 0;
    }
    .tagline {
      margin: 12px 0 0;
      color: #C7D4F5;
      font-size: 23px;
      line-height: 1.2;
      font-weight: 600;
    }
    .chips {
      display: flex;
      gap: 18px;
      margin-top: 24px;
    }
    .chip {
      min-width: 158px;
      padding: 9px 24px;
      border: 1px solid #314573;
      border-radius: 999px;
      color: #CED9F6;
      background: #101B32;
      font-size: 19px;
      font-weight: 700;
      text-align: center;
    }
    .flow {
      display: grid;
      grid-template-columns: 260px 62px 322px 62px 304px;
      align-items: center;
      gap: 0;
      margin-top: 28px;
    }
    .card {
      min-height: 132px;
      padding: 22px 28px;
      border: 1px solid #31508A;
      border-radius: 24px;
      background:
        linear-gradient(135deg, rgba(22, 38, 72, 0.98), rgba(12, 21, 42, 0.98)),
        linear-gradient(135deg, transparent 0 62%, rgba(64, 28, 94, 0.18) 62% 100%);
    }
    .card-title {
      display: flex;
      align-items: center;
      gap: 14px;
      color: #F6F9FF;
      font-size: 23px;
      font-weight: 700;
      line-height: 1.1;
    }
    .card-title span {
      display: block;
      width: 18px;
      height: 18px;
      border-radius: 999px;
    }
    ul {
      margin: 18px 0 0;
      padding: 0;
      list-style: none;
      color: #B7C5EB;
      font-size: 17px;
      line-height: 1.35;
      font-weight: 700;
    }
    .arrow {
      color: #5C8DFF;
      font-size: 58px;
      font-weight: 500;
      text-align: center;
    }
    .footer-pill {
      position: absolute;
      left: 300px;
      right: 300px;
      bottom: 25px;
      color: #B7C5EB;
      font-size: 18px;
      line-height: 1.2;
      font-weight: 700;
      text-align: center;
    }
    .url,
    .meta {
      position: absolute;
      bottom: 25px;
      color: #91A8DD;
      font-size: 20px;
      font-weight: 700;
    }
    .url { left: 42px; }
    .meta { right: 42px; }
  </style>
</head>
<body>
  <main class="canvas">
    <section class="frame">
      <div class="brand">
        <div class="logo-tile"><img src="${logoData}" alt="" /></div>
        <div>
          <h1>Alfred</h1>
          <p class="tagline">Autonomous repo teammates for Claude Code and Codex.</p>
        </div>
      </div>
      <div class="chips">
        <div class="chip">GitHub-native</div>
        <div class="chip">multi-repo</div>
        <div class="chip">spec-driven</div>
      </div>
      <div class="flow">
        ${card("Scope", ["GitHub issues", "specs", "PR feedback"], "#47D18C")}
        <div class="arrow">→</div>
        ${card("Alfred runtime", ["scheduler", "state machine", "clean worktree", "engine routing"], "#5B8CFF")}
        <div class="arrow">→</div>
        ${card("Engines + output", ["Claude Code or Codex", "PRs, reviews, tests", "Slack summaries"], "#F4B43E")}
      </div>
      <div class="footer-pill">Planned, claimed, reviewed, and reported back.</div>
      <div class="url">alfred.luminik.io</div>
      <div class="meta">MIT · Python · macOS/Linux</div>
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
