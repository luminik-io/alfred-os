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
      background: #060b16;
      color: #f8fbff;
      font-family: Quicksand, Arial, sans-serif;
      -webkit-font-smoothing: antialiased;
      text-rendering: geometricPrecision;
    }
    .canvas {
      position: relative;
      width: 1200px;
      height: 630px;
      padding: 58px 74px 56px;
      background:
        radial-gradient(ellipse 620px 420px at 84% 42%, rgba(44, 111, 255, 0.34), transparent 66%),
        radial-gradient(ellipse 720px 500px at 6% 0%, rgba(50, 98, 210, 0.30), transparent 60%),
        linear-gradient(135deg, #081327 0%, #030812 58%, #05070d 100%);
    }
    .canvas::before {
      content: "";
      position: absolute;
      inset: 0;
      opacity: 0.34;
      background-image:
        linear-gradient(rgba(123, 154, 224, 0.14) 1px, transparent 1px),
        linear-gradient(90deg, rgba(123, 154, 224, 0.14) 1px, transparent 1px);
      background-size: 150px 120px;
      mask-image: linear-gradient(90deg, transparent 0%, #000 16%, #000 84%, transparent 100%);
    }
    .content {
      position: relative;
      z-index: 1;
      height: 100%;
      display: flex;
      flex-direction: column;
    }
    .top {
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .brand img {
      width: 42px;
      height: 42px;
      object-fit: contain;
    }
    .brand .wordmark {
      font-size: 30px;
      font-weight: 700;
      letter-spacing: 0;
      color: #F2F6FF;
    }
    .meta {
      font-size: 18px;
      font-weight: 600;
      letter-spacing: 0;
      color: #AFC3F7;
    }
    .main {
      flex: 1;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 54px;
      padding: 30px 0 20px;
    }
    .copy {
      width: 690px;
      flex: none;
    }
    h1 {
      margin: 0;
      font-size: 72px;
      line-height: 1.05;
      font-weight: 700;
      letter-spacing: 0;
      color: #FFFFFF;
    }
    h1 .accent {
      color: #93B6FF;
    }
    .sub {
      margin: 26px 0 0;
      max-width: 680px;
      color: #B6C7EE;
      font-size: 25px;
      line-height: 1.38;
      font-weight: 500;
      letter-spacing: 0;
    }
    .sub .em {
      color: #D4E1FF;
      font-weight: 700;
    }
    .mark {
      position: relative;
      width: 330px;
      height: 330px;
      flex: none;
      border-radius: 42px;
      display: grid;
      place-items: center;
      background:
        linear-gradient(145deg, rgba(22, 34, 63, 0.78), rgba(5, 10, 23, 0.58));
      border: 1px solid rgba(134, 166, 242, 0.22);
      box-shadow:
        0 34px 90px rgba(0, 0, 0, 0.32),
        inset 0 1px 0 rgba(255, 255, 255, 0.08);
      overflow: hidden;
    }
    .mark::before {
      content: "";
      position: absolute;
      width: 260px;
      height: 260px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(74, 120, 255, 0.34), transparent 68%);
    }
    .mark img {
      position: relative;
      width: 232px;
      height: 232px;
      object-fit: contain;
      filter: drop-shadow(0 18px 30px rgba(14, 66, 210, 0.38));
    }
    .flow {
      display: grid;
      grid-template-columns: 1fr 36px 1fr 36px 1fr;
      align-items: stretch;
      gap: 12px;
      min-height: 92px;
    }
    .step {
      border: 1px solid rgba(134, 166, 242, 0.24);
      background: rgba(17, 29, 57, 0.68);
      border-radius: 22px;
      padding: 16px 18px;
    }
    .step span {
      display: block;
      margin-bottom: 6px;
      color: #7F96CA;
      font-size: 15px;
      line-height: 1;
      font-weight: 700;
      letter-spacing: 0;
    }
    .step strong {
      display: block;
      color: #E9F0FF;
      font-size: 20px;
      line-height: 1.18;
      font-weight: 700;
      letter-spacing: 0;
    }
    .arrow {
      display: grid;
      place-items: center;
      color: #7EA6FF;
      font-size: 30px;
      font-weight: 700;
    }
    .footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
      color: #6E84B0;
      font-size: 17px;
      font-weight: 600;
      letter-spacing: 0.3px;
    }
    .footer .url {
      color: #B6C7EE;
    }
    .rule {
      flex: 1;
      height: 1px;
      margin: 0 22px;
      background: linear-gradient(90deg, rgba(110, 132, 176, 0) 0%, rgba(110, 132, 176, 0.45) 50%, rgba(110, 132, 176, 0) 100%);
    }
  </style>
</head>
<body>
  <main class="canvas">
    <div class="content">
      <div class="top">
        <div class="brand">
          <img src="${logoData}" alt="" />
          <span class="wordmark">Alfred</span>
        </div>
        <div class="meta">local · open source · MIT</div>
      </div>
      <div class="main">
        <section class="copy">
          <h1>GitHub issues become <span class="accent">reviewed pull requests.</span></h1>
          <p class="sub">Alfred runs Claude Code and Codex locally as autonomous development teammates, with specs, clean worktrees, and Slack reports.</p>
        </section>
        <section class="mark" aria-hidden="true">
          <img src="${logoData}" alt="" />
        </section>
      </div>
      <div class="flow" aria-hidden="true">
        <div class="step"><span>Scope</span><strong>issues + specs</strong></div>
        <div class="arrow">→</div>
        <div class="step"><span>Run</span><strong>local agents</strong></div>
        <div class="arrow">→</div>
        <div class="step"><span>Return</span><strong>PRs + reviews</strong></div>
      </div>
      <div class="footer">
        <div class="url">alfred.luminik.io</div>
        <div class="rule"></div>
        <div>luminik-io/alfred-os · macOS/Linux</div>
      </div>
    </div>
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
      "--virtual-time-budget=1500",
      "--force-device-scale-factor=2",
      "--window-size=1200,630",
      `--screenshot=${screenshotPath}`,
      `file://${htmlPath}`,
    ]);

    await mkdir(dirname(outPath), { recursive: true });
    await sharp(screenshotPath).resize(1200, 630).png({ compressionLevel: 9 }).toFile(outPath);
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
