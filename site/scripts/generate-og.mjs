import { mkdir, readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import sharp from "sharp";

const root = resolve(import.meta.dirname, "..");
const logoPath = resolve(root, "src/assets/alfred-logo.png");
const outPath = resolve(root, "public/brand/alfred-og.png");
const fontDir = resolve(root, "node_modules/@fontsource/quicksand/files");
const fontFamily = "Quicksand, Arial, sans-serif";

async function fontFace(weight) {
  const fontPath = resolve(fontDir, `quicksand-latin-${weight}-normal.woff2`);
  try {
    const font = await readFile(fontPath);
    return `@font-face{font-family:Quicksand;font-style:normal;font-weight:${weight};src:url(data:font/woff2;base64,${font.toString("base64")}) format("woff2");}`;
  } catch {
    return "";
  }
}

const logo = await readFile(logoPath);
const logoData = `data:image/png;base64,${logo.toString("base64")}`;
const fontFaceCss = (await Promise.all([400, 500, 600, 700].map(fontFace))).filter(Boolean).join("");

const svg = `<?xml version="1.0" encoding="UTF-8"?>
<svg width="1200" height="630" viewBox="0 0 1200 630" fill="none" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <style>${fontFaceCss}</style>
    <radialGradient id="blueGlow" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(260 210) rotate(35) scale(450 360)">
      <stop stop-color="#245DFF" stop-opacity="0.45"/>
      <stop offset="1" stop-color="#245DFF" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="amberGlow" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(1000 500) rotate(15) scale(330 240)">
      <stop stop-color="#F4B43E" stop-opacity="0.22"/>
      <stop offset="1" stop-color="#F4B43E" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="panel" x1="690" y1="132" x2="1050" y2="480" gradientUnits="userSpaceOnUse">
      <stop stop-color="#121A2C"/>
      <stop offset="1" stop-color="#0B1020"/>
    </linearGradient>
    <linearGradient id="stroke" x1="78" y1="62" x2="1126" y2="574" gradientUnits="userSpaceOnUse">
      <stop stop-color="#2D6CFF"/>
      <stop offset="0.5" stop-color="#5D7DFF" stop-opacity="0.48"/>
      <stop offset="1" stop-color="#F4B43E" stop-opacity="0.45"/>
    </linearGradient>
    <filter id="softShadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="24" stdDeviation="28" flood-color="#000000" flood-opacity="0.32"/>
    </filter>
  </defs>

  <rect width="1200" height="630" fill="#070B14"/>
  <rect width="1200" height="630" fill="url(#blueGlow)"/>
  <rect width="1200" height="630" fill="url(#amberGlow)"/>

  <g opacity="0.18">
    <path d="M80 126H1120" stroke="#8EA8FF"/>
    <path d="M80 234H1120" stroke="#8EA8FF"/>
    <path d="M80 342H1120" stroke="#8EA8FF"/>
    <path d="M80 450H1120" stroke="#8EA8FF"/>
    <path d="M224 70V560" stroke="#8EA8FF"/>
    <path d="M440 70V560" stroke="#8EA8FF"/>
    <path d="M656 70V560" stroke="#8EA8FF"/>
    <path d="M872 70V560" stroke="#8EA8FF"/>
    <path d="M1088 70V560" stroke="#8EA8FF"/>
  </g>

  <rect x="54" y="48" width="1092" height="534" rx="34" stroke="url(#stroke)" stroke-width="2"/>

  <text x="1106" y="88" fill="#90A4D8" font-family="${fontFamily}" font-size="18" font-weight="600" text-anchor="end">alfred.luminik.io</text>

  <g filter="url(#softShadow)">
    <rect x="92" y="98" width="258" height="258" rx="42" fill="#080D18" stroke="#25314E"/>
    <image href="${logoData}" x="113" y="119" width="216" height="216" preserveAspectRatio="xMidYMid meet"/>
  </g>

  <text x="92" y="466" fill="#F7FAFF" font-family="${fontFamily}" font-size="92" font-weight="700">Alfred</text>
  <text x="96" y="528" fill="#C8D3F5" font-family="${fontFamily}" font-size="31" font-weight="500">A local AI agent fleet for solo builders.</text>

  <g filter="url(#softShadow)">
    <rect x="682" y="112" width="424" height="372" rx="30" fill="url(#panel)" stroke="#2D3B5D"/>
    <text x="724" y="174" fill="#F7FAFF" font-family="${fontFamily}" font-size="34" font-weight="700">Run a local agent fleet</text>
    <text x="724" y="216" fill="#B9C7EA" font-family="${fontFamily}" font-size="24" font-weight="500">from one machine you own.</text>

    <g font-family="${fontFamily}" font-size="23" font-weight="600">
      <rect x="724" y="262" width="318" height="46" rx="23" fill="#13213D" stroke="#314676"/>
      <circle cx="750" cy="285" r="7" fill="#47D18C"/>
      <text x="770" y="293" fill="#E8EEFF">GitHub issue claimed</text>

      <rect x="724" y="324" width="318" height="46" rx="23" fill="#13213D" stroke="#314676"/>
      <circle cx="750" cy="347" r="7" fill="#5B8CFF"/>
      <text x="770" y="355" fill="#E8EEFF">Isolated worktree</text>

      <rect x="724" y="386" width="318" height="46" rx="23" fill="#13213D" stroke="#314676"/>
      <circle cx="750" cy="409" r="7" fill="#F4B43E"/>
      <text x="770" y="417" fill="#E8EEFF">PR, review, Slack report</text>
    </g>
  </g>

  <g font-family="${fontFamily}" font-size="22" font-weight="700">
    <rect x="683" y="512" width="126" height="42" rx="21" fill="#0E172A" stroke="#304164"/>
    <text x="714" y="540" fill="#B9C7EA">Claude</text>
    <rect x="825" y="512" width="111" height="42" rx="21" fill="#0E172A" stroke="#304164"/>
    <text x="857" y="540" fill="#B9C7EA">Codex</text>
    <rect x="952" y="512" width="154" height="42" rx="21" fill="#0E172A" stroke="#304164"/>
    <text x="983" y="540" fill="#B9C7EA">launchd</text>
  </g>
</svg>`;

await mkdir(dirname(outPath), { recursive: true });
await sharp(Buffer.from(svg)).png().toFile(outPath);
console.log(`wrote ${outPath}`);
