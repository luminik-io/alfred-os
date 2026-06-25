import { Check, Copy, ExternalLink, RotateCcw } from "lucide-react";
import type { ComponentPropsWithoutRef, ReactNode } from "react";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

import { isSafeExternalUrl, openExternal } from "../lib/links";

// One chat message: an avatar-less bubble with a role label, the rendered body,
// and a hover/focus action bar (assistant-ui's Message + ActionBar pattern).
// Assistant replies render as rich markdown with syntax-highlighted, copyable
// code blocks; user turns stay plain text so typed prose can never smuggle
// markup. The action bar exposes copy-message and, on the most recent assistant
// turn, retry/regenerate.

export type ChatMessageProps = {
  role: "user" | "assistant";
  content: string;
  // True while tokens are still streaming into this turn: shows the typing
  // indicator (when empty) and a live caret, and suppresses the action bar.
  streaming?: boolean;
  // Show the retry/regenerate control. Only the latest assistant turn passes
  // this so the bar stays uncluttered.
  canRetry?: boolean;
  onRetry?: () => void;
};

export function ChatMessage({
  role,
  content,
  streaming = false,
  canRetry = false,
  onRetry,
}: ChatMessageProps) {
  const who = role === "user" ? "You" : "Alfred";
  const showActions = !streaming && content.trim().length > 0;
  const streamingEmpty = streaming && content.length === 0;

  return (
    <div className={`ask-bubble ask-bubble--${role}`}>
      <div className="ask-bubble__head">
        <span className="ask-bubble__who">{who}</span>
        {showActions ? (
          <div className="ask-bubble__actions">
            <CopyButton value={content} label="Copy message" />
            {role === "assistant" && canRetry && onRetry ? (
              <button
                type="button"
                className="ask-bubble__action"
                onClick={onRetry}
                aria-label="Regenerate this reply"
                title="Regenerate"
              >
                <RotateCcw size={13} aria-hidden="true" />
              </button>
            ) : null}
          </div>
        ) : null}
      </div>

      {streamingEmpty ? (
        <span className="ask-bubble__pending" aria-label="Alfred is thinking" role="status">
          <span className="ask-bubble__dot" />
          <span className="ask-bubble__dot" />
          <span className="ask-bubble__dot" />
        </span>
      ) : role === "assistant" ? (
        <div className={`ask-bubble__md${streaming ? " ask-bubble__md--streaming" : ""}`}>
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]}
            components={{
              a: ({ href, children }) => (
                <SafeMarkdownLink href={href}>{children}</SafeMarkdownLink>
              ),
              pre: CodeBlock,
            }}
          >
            {content}
          </ReactMarkdown>
        </div>
      ) : (
        <p className="ask-bubble__text">{content}</p>
      )}
    </div>
  );
}

// A copy control with a transient "copied" confirmation. Shared by the message
// action bar and each code block.
function CopyButton({
  value,
  label,
  className = "ask-bubble__action",
}: {
  value: string;
  label: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard?.writeText(value).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    });
  };
  return (
    <button
      type="button"
      className={className}
      onClick={copy}
      aria-label={copied ? "Copied" : label}
      title={copied ? "Copied" : label}
    >
      {copied ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
    </button>
  );
}

// A fenced code block with a header (language label + copy-code button). The
// inner <code> keeps the hljs classes rehype-highlight applies so the theme in
// index.css colours the tokens.
function CodeBlock({ children, ...rest }: ComponentPropsWithoutRef<"pre">) {
  const { code, language } = readCodeChild(children);
  return (
    <div className="ask-code">
      <div className="ask-code__bar">
        <span className="ask-code__lang">{language || "code"}</span>
        <CopyButton value={code} label="Copy code" className="ask-code__copy" />
      </div>
      <pre {...rest}>{children}</pre>
    </div>
  );
}

// Pull the raw text and the highlight.js language out of the <code> element
// react-markdown nests inside a <pre>. The language rides as a `language-xxx`
// (or `hljs language-xxx`) class on the code element.
function readCodeChild(children: ReactNode): { code: string; language: string } {
  let code = "";
  let language = "";
  const child = Array.isArray(children) ? children[0] : children;
  if (child && typeof child === "object" && "props" in child) {
    const props = (child as { props?: { className?: string; children?: ReactNode } }).props;
    code = nodeText(props?.children);
    const match = /language-([\w-]+)/.exec(props?.className || "");
    if (match) language = match[1];
  } else {
    code = nodeText(children);
  }
  return { code: code.replace(/\n$/, ""), language };
}

function nodeText(node: ReactNode): string {
  if (node == null || node === false || node === true) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (typeof node === "object" && "props" in node) {
    return nodeText((node as { props?: { children?: ReactNode } }).props?.children);
  }
  return "";
}

function SafeMarkdownLink({ href, children }: { href?: string; children: ReactNode }) {
  if (!href || !isSafeExternalUrl(href)) {
    return <span className="ask-bubble__unsafe-link">{children}</span>;
  }
  return (
    <button
      className="ask-bubble__link"
      type="button"
      onClick={() => void openExternal(href)}
    >
      <span>{children}</span>
      <ExternalLink size={13} aria-hidden="true" />
    </button>
  );
}
