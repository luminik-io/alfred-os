import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { isSafeExternalUrl, openExternal } from "../lib/links";

/**
 * Shared markdown renderer. Headings, lists, tables, and fenced code render
 * like a real document instead of a raw dump. Links are rendered as safe
 * buttons that open through the platform handler, never as raw anchors, so a
 * plan body authored elsewhere cannot smuggle a navigation target.
 */
export function Markdown({
  children,
  className,
}: {
  children: string;
  className: string;
}) {
  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => (
            <SafeMarkdownLink href={href}>{children}</SafeMarkdownLink>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

function SafeMarkdownLink({ href, children }: { href?: string; children: ReactNode }) {
  if (!href || !isSafeExternalUrl(href)) {
    return <span className="md__unsafe-link">{children}</span>;
  }
  return (
    <button className="md__link" type="button" onClick={() => void openExternal(href)}>
      {children}
    </button>
  );
}
