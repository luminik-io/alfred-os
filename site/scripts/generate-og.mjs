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
      -webkit-font-smoothing: antialiased;
      text-rendering: geometricPrecision;
    }
    .canvas {
      position: relative;
      width: 1200px;
      height: 630px;
      padding: 72px 88px 64px;
      background:
        radial-gradient(ellipse 720px 540px at 12% 8%, rgba(58, 124, 255, 0.30), transparent 65%),
        linear-gradient(180deg, #060d1c 0%, #03070f 100%);
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
      gap: 16px;
    }
    .brand img {
      width: 44px;
      height: 44px;
      object-fit: contain;
    }
    .brand .wordmark {
      font-size: 28px;
      font-weight: 700;
      letter-spacing: 0;
      color: #F2F6FF;
    }
    .meta {
      font-size: 16px;
      font-weight: 600;
      letter-spacing: 0.4px;
      color: #6E84B0;
      text-transform: lowercase;
    }
    .hero {
      flex: 1;
      display: flex;
      flex-direction: column;
      justify-content: center;
      margin-top: -16px;
    }
    h1 {
      margin: 0;
      max-width: 1020px;
      font-size: 92px;
      line-height: 1.02;
      font-weight: 700;
      letter-spacing: -1.5px;
      color: #FFFFFF;
    }
    h1 .turn {
      color: #93B6FF;
    }
    .sub {
      margin: 32px 0 0;
      max-width: 880px;
      color: #B6C7EE;
      font-size: 26px;
      line-height: 1.4;
      font-weight: 500;
      letter-spacing: -0.1px;
    }
    .sub .em {
      color: #D4E1FF;
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
        <div class="meta">open source · mit license</div>
      </div>
      <div class="hero">
        <h1>GitHub issues, in.<br /><span class="turn">Pull requests, out.</span></h1>
        <p class="sub">A self-hosted runtime for <span class="em">autonomous</span> Claude Code and Codex agents that turn scoped issues into reviewed pull requests, on the CLI subscriptions you already pay for.</p>
      </div>
      <div class="footer">
        <div class="url">alfred.luminik.io</div>
        <div class="rule"></div>
        <div>luminik-io/alfred-os</div>
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
