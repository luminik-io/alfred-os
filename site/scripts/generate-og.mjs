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
      padding: 72px 88px;
      background:
        radial-gradient(circle at 18% 12%, rgba(56, 124, 255, 0.42), transparent 44%),
        radial-gradient(circle at 88% 92%, rgba(244, 180, 62, 0.18), transparent 40%),
        linear-gradient(180deg, #081227 0%, #050913 100%);
    }
    .canvas::before {
      content: "";
      position: absolute;
      inset: 0;
      opacity: 0.18;
      background-image:
        linear-gradient(rgba(94, 137, 239, 0.16) 1px, transparent 1px),
        linear-gradient(90deg, rgba(94, 137, 239, 0.16) 1px, transparent 1px);
      background-size: 60px 60px;
      mask-image: radial-gradient(circle at 50% 50%, black 55%, transparent 85%);
      -webkit-mask-image: radial-gradient(circle at 50% 50%, black 55%, transparent 85%);
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
      gap: 18px;
    }
    .brand img {
      width: 56px;
      height: 56px;
      object-fit: contain;
    }
    .brand .name {
      font-size: 34px;
      font-weight: 700;
      letter-spacing: 0.5px;
      color: #F8FBFF;
    }
    .hero {
      max-width: 1000px;
      margin-top: -8px;
    }
    .hero h1 {
      margin: 0;
      font-size: 76px;
      line-height: 1.04;
      font-weight: 700;
      letter-spacing: -0.5px;
      color: #FFFFFF;
    }
    .hero h1 .accent {
      color: #7BA6FF;
    }
    .hero p {
      margin: 28px 0 0;
      max-width: 880px;
      color: #BFD0F2;
      font-size: 28px;
      line-height: 1.38;
      font-weight: 500;
    }
    .footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 24px;
      color: #8FA5D6;
      font-size: 20px;
      font-weight: 600;
      letter-spacing: 0.3px;
    }
    .footer .meta {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .footer .dot {
      width: 4px;
      height: 4px;
      border-radius: 999px;
      background: #3F5587;
      display: inline-block;
    }
  </style>
</head>
<body>
  <main class="canvas">
    <div class="content">
      <div class="brand">
        <img src="${logoData}" alt="" />
        <span class="name">Alfred</span>
      </div>
      <div class="hero">
        <h1>Autonomous engineering agents,<br /><span class="accent">on your own machine.</span></h1>
        <p>GitHub issues become pull requests through Claude Code and Codex you already pay for. Scheduled, sandboxed, and reported back to Slack.</p>
      </div>
      <div class="footer">
        <div>alfred.luminik.io</div>
        <div class="meta">
          <span>MIT</span>
          <span class="dot"></span>
          <span>Python</span>
          <span class="dot"></span>
          <span>macOS &amp; Linux</span>
        </div>
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
