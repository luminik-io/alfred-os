import { Check, Copy, ExternalLink, RotateCcw } from "lucide-react";
import type { ComponentPropsWithoutRef, ReactNode } from "react";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import {
  MessagePrimitive,
  useMessage,
  type TextMessagePartComponent,
} from "@assistant-ui/react";

import { isSafeExternalUrl, openExternal } from "../../lib/links";
import { AskDraftPart } from "./AskDraftPart";
import { DRAFT_TOOL_NAME } from "./askModel";

// The Ask message surface, built from assistant-ui MessagePrimitive but styled
// with Alfred's existing tokens so it is visually identical to the prior
// hand-rolled bubbles. User turns stay plain text so typed prose can never
// smuggle markup; assistant replies render as rich markdown with
// syntax-highlighted, copyable code blocks. Draft/plan cards ride as a custom
// "alfred-draft" tool-call part, rendered inline by AskDraftPart.

// Whether the role being rendered is the most recent settled assistant message,
// supplied by the view so only that turn shows the regenerate control.
export type AskMessageContext = {
  busy: boolean;
  canRetry: boolean;
  onRetry: () => void;
  // The id of the last assistant text reply, so only that bubble shows the
  // regenerate control even when a draft card trails it as a separate message.
  lastReplyId: string | null;
};

// The text part renderer: rich markdown for assistant prose, plain text for the
// user. A streaming assistant part shows a live caret; an empty streaming part
// shows the three-dot typing indicator.
function AskTextPart(role: "user" | "assistant"): TextMessagePartComponent {
  return function TextPart({ text, status }) {
    const streaming = status?.type === "running";
    if (role === "user") {
      return <p className="ask-bubble__text">{text}</p>;
    }
    if (streaming && text.length === 0) {
      return (
        <span className="ask-bubble__pending" aria-label="Alfred is thinking" role="status">
          <span className="ask-bubble__dot" />
          <span className="ask-bubble__dot" />
          <span className="ask-bubble__dot" />
        </span>
      );
    }
    return (
      <div className={`ask-bubble__md${streaming ? " ask-bubble__md--streaming" : ""}`}>
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]}
          components={{
            a: ({ href, children }) => <SafeMarkdownLink href={href}>{children}</SafeMarkdownLink>,
            pre: CodeBlock,
          }}
        >
          {text}
        </ReactMarkdown>
      </div>
    );
  };
}

const UserTextPart = AskTextPart("user");
const AssistantTextPart = AskTextPart("assistant");

// Stable across renders: recreating this map every render remounts the part
// renderers on every streamed token.
const ASSISTANT_MESSAGE_PARTS = {
  Text: AssistantTextPart,
  tools: { by_name: { [DRAFT_TOOL_NAME]: AskDraftPart } },
};

export function AskUserMessage() {
  return (
    <MessagePrimitive.Root className="ask-bubble ask-bubble--user">
      <div className="ask-bubble__head">
        <span className="ask-bubble__who">You</span>
        <div className="ask-bubble__actions">
          <CopyMessageButton />
        </div>
      </div>
      <MessagePrimitive.Parts components={{ Text: UserTextPart }} />
    </MessagePrimitive.Root>
  );
}

export function AskAssistantMessage({ context }: { context: AskMessageContext }) {
  const message = useMessage();
  // A draft-only assistant message renders just the inline card (no "Alfred"
  // header, no action bar): it is an offer attached to a build turn, not a
  // chat bubble. Detect it by its single alfred-draft tool-call part.
  const isDraftOnly =
    message.content.length === 1 &&
    message.content[0]?.type === "tool-call" &&
    message.content[0]?.toolName === DRAFT_TOOL_NAME;

  if (isDraftOnly) {
    return (
      <MessagePrimitive.Root>
        <MessagePrimitive.Parts
          components={{ tools: { by_name: { [DRAFT_TOOL_NAME]: AskDraftPart } } }}
        />
      </MessagePrimitive.Root>
    );
  }

  const streaming = message.status?.type === "running";
  const hasText = message.content.some(
    (part) => part.type === "text" && part.text.trim().length > 0,
  );
  const showActions = !streaming && hasText;

  return (
    <MessagePrimitive.Root className="ask-bubble ask-bubble--assistant">
      <div className="ask-bubble__head">
        <span className="ask-bubble__who">Alfred</span>
        {showActions ? (
          <div className="ask-bubble__actions">
            <CopyMessageButton />
            {context.canRetry && message.id === context.lastReplyId ? (
              // Regenerate replays the last user turn, so only offer it on the
              // last assistant TEXT reply. `If last` would hide it whenever a
              // draft card (a separate trailing message) follows the reply, so
              // the view passes the id of the last reply to gate on instead.
              <button
                type="button"
                className="ask-bubble__action"
                onClick={context.onRetry}
                aria-label="Regenerate this reply"
                title="Regenerate"
              >
                <RotateCcw size={13} aria-hidden="true" />
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
      <MessagePrimitive.Parts components={ASSISTANT_MESSAGE_PARTS} />
    </MessagePrimitive.Root>
  );
}

// Copy the full rendered message text. Reads the message's text parts directly
// so the clipboard carries exactly what the person sees.
function CopyMessageButton() {
  const message = useMessage();
  const value = message.content
    .filter((part): part is { type: "text"; text: string } => part.type === "text")
    .map((part) => part.text)
    .join("\n");
  return <CopyButton value={value} label="Copy message" />;
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
// react-markdown nests inside a <pre>.
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
    <button className="ask-bubble__link" type="button" onClick={() => void openExternal(href)}>
      <span>{children}</span>
      <ExternalLink size={13} aria-hidden="true" />
    </button>
  );
}
