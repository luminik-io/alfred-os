import { openUrl } from "@tauri-apps/plugin-opener";

export function localUrl(baseUrl: string, path: string): string {
  try {
    const url = new URL(baseUrl);
    url.pathname = path;
    url.search = "";
    url.hash = "";
    return url.toString();
  } catch {
    return path;
  }
}

export function firstLink(text: string, matcher: RegExp): string | null {
  const urls = text.match(/https?:\/\/[^\s)]+/g) || [];
  return urls.find((url) => matcher.test(url)) || null;
}

export function isSafeExternalUrl(href: string): boolean {
  try {
    const url = new URL(href);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

export async function openExternal(href: string): Promise<void> {
  if (!isSafeExternalUrl(href)) {
    return;
  }
  if (window.__TAURI_INTERNALS__) {
    await openUrl(href);
    return;
  }
  window.open(href, "_blank", "noopener,noreferrer");
}
