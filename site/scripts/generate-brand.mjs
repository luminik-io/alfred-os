import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import sharp from "sharp";

const root = resolve(import.meta.dirname, "../..");
const siteRoot = resolve(import.meta.dirname, "..");

const outputs = [
  {
    name: "alfred-logo-transparent.png",
    theme: "transparent",
    paths: [
      resolve(root, "assets/brand/alfred-logo-transparent.png"),
      resolve(siteRoot, "public/brand/alfred-logo-transparent.png"),
      resolve(siteRoot, "src/assets/alfred-logo-transparent.png"),
    ],
  },
  {
    name: "alfred-logo.png",
    theme: "dark",
    paths: [
      resolve(root, "assets/brand/alfred-logo.png"),
      resolve(siteRoot, "public/brand/alfred-logo.png"),
      resolve(siteRoot, "src/assets/alfred-logo.png"),
      resolve(siteRoot, "public/favicon.png"),
    ],
  },
  {
    name: "alfred-logo-light.png",
    theme: "light",
    paths: [
      resolve(root, "assets/brand/alfred-logo-light.png"),
      resolve(siteRoot, "public/brand/alfred-logo-light.png"),
    ],
  },
];

const appleIconPath = resolve(siteRoot, "public/apple-touch-icon.png");

function logoSvg(theme) {
  const isDark = theme === "dark";
  const isLight = theme === "light";
  const background = isDark
    ? '<rect width="512" height="512" rx="84" fill="#050A12"/><rect x="1" y="1" width="510" height="510" rx="83" fill="none" stroke="#17223B" stroke-width="2"/>'
    : isLight
      ? '<rect width="512" height="512" rx="84" fill="#F4F7FB"/><rect x="1" y="1" width="510" height="510" rx="83" fill="none" stroke="#D6DFEF" stroke-width="2"/>'
      : "";

  const shadowOpacity = theme === "transparent" ? "0.16" : isLight ? "0.12" : "0.34";
  const eyeOuter = isLight ? "#F7FAFF" : "#F4F7FB";
  const eyeRing = isLight ? "#B9C7E5" : "#C7D2E8";

  return `<?xml version="1.0" encoding="UTF-8"?>
<svg width="512" height="512" viewBox="0 0 512 512" fill="none" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="blueFill" x1="91" y1="177" x2="414" y2="316" gradientUnits="userSpaceOnUse">
      <stop stop-color="#2D7DFF"/>
      <stop offset="0.52" stop-color="#1263F5"/>
      <stop offset="1" stop-color="#2E88FF"/>
    </linearGradient>
    <linearGradient id="blueStroke" x1="101" y1="72" x2="426" y2="442" gradientUnits="userSpaceOnUse">
      <stop stop-color="#3293FF"/>
      <stop offset="0.52" stop-color="#1968F5"/>
      <stop offset="1" stop-color="#0C48C6"/>
    </linearGradient>
    <filter id="markShadow" x="-16%" y="-16%" width="132%" height="132%">
      <feDropShadow dx="0" dy="18" stdDeviation="16" flood-color="#000717" flood-opacity="${shadowOpacity}"/>
    </filter>
    <filter id="smallShadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="6" stdDeviation="5" flood-color="#000717" flood-opacity="${shadowOpacity}"/>
    </filter>
  </defs>
  ${background}
  <g filter="url(#markShadow)">
    <path d="M113 157V99L256 45L399 99V157" stroke="url(#blueStroke)" stroke-width="11" stroke-linecap="square" stroke-linejoin="miter"/>
    <path d="M94 232C111 331 166 410 256 462C346 410 401 331 418 232" stroke="url(#blueStroke)" stroke-width="11" stroke-linecap="square"/>
    <path d="M256 462C166 410 111 331 94 232" stroke="#0B3FBA" stroke-opacity="0.45" stroke-width="3"/>
    <path d="M256 462C346 410 401 331 418 232" stroke="#4DA2FF" stroke-opacity="0.35" stroke-width="3"/>
    <path d="M42 244C83 203 129 177 178 164L160 214L213 228L249 190V301L226 260C212 251 197 248 179 249L172 268L124 259L115 242C91 240 67 241 42 244Z" fill="url(#blueFill)" stroke="#77B4FF" stroke-width="1.4" stroke-linejoin="round"/>
    <path d="M470 244C429 203 383 177 334 164L352 214L299 228L263 190V301L286 260C300 251 315 248 333 249L340 268L388 259L397 242C421 240 445 241 470 244Z" fill="url(#blueFill)" stroke="#77B4FF" stroke-width="1.4" stroke-linejoin="round"/>
    <path d="M249 190L256 220L263 190V301L256 284L249 301V190Z" fill="#0A2E93" opacity="0.82"/>
    <path d="M42 244C89 229 125 230 162 244" stroke="#031737" stroke-opacity="0.42" stroke-width="5"/>
    <path d="M470 244C423 229 387 230 350 244" stroke="#031737" stroke-opacity="0.42" stroke-width="5"/>
    <circle cx="256" cy="230" r="31" fill="#020A14" opacity="0.78"/>
    <circle cx="256" cy="230" r="27" fill="${eyeOuter}" stroke="${eyeRing}" stroke-width="5"/>
    <circle cx="256" cy="230" r="14" fill="#06162A" stroke="#0B2F68" stroke-width="5"/>
    <circle cx="256" cy="230" r="5" fill="#4EA0FF"/>
    <circle cx="256" cy="352" r="10" fill="#2A7CFF" stroke="#66ADFF" stroke-width="2" filter="url(#smallShadow)"/>
    <path d="M256 373V402" stroke="#2A7CFF" stroke-width="4" stroke-linecap="square"/>
  </g>
  <path d="M121 102L256 51L391 102" stroke="#7DB9FF" stroke-opacity="0.36" stroke-width="3"/>
</svg>`;
}

async function writePng(path, svg, size = 512) {
  await mkdir(dirname(path), { recursive: true });
  await sharp(Buffer.from(svg)).resize(size, size).png().toFile(path);
}

for (const output of outputs) {
  const svg = logoSvg(output.theme);
  await Promise.all(output.paths.map((path) => writePng(path, svg)));
  console.log(`wrote ${output.name}`);
}

await writePng(appleIconPath, logoSvg("dark"), 180);
console.log("wrote apple-touch-icon.png");
