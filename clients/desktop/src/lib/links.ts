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

/** Parse "owner/repo#123" or a GitHub issue URL into a repo + number. */
export function parseIssueRef(text: string): { repo: string; number: number } | null {
  const trimmed = text.trim();
  const url = trimmed.match(/github\.com\/([\w.-]+\/[\w.-]+)\/issues\/(\d+)/i);
  if (url) return { repo: url[1], number: Number(url[2]) };
  const slug = trimmed.match(/^([\w.-]+\/[\w.-]+)#(\d+)$/);
  if (slug) return { repo: slug[1], number: Number(slug[2]) };
  return null;
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
