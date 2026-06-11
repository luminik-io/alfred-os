// Render one raw stream-json transcript line (as teed by the runtime's Claude
// streaming path) into a compact, human-readable tail line. Mirrors the
// server's `transcripts._render_event` so the live tail reads the same as the
// post-hoc `alfred logs` view: system/assistant/tool_use/tool_result/result.
//
// The live log tail (#41) streams these raw JSONL strings; this turns each into
// the `{ ts, text }` shape the EventTail list already renders. A line that does
// not parse, or that carries nothing worth showing (an empty assistant turn),
// returns null so the caller can skip it.

export type TranscriptTailLine = { ts: string | null; text: string };

export function formatTranscriptLine(raw: string): TranscriptTailLine | null {
  const trimmed = raw.trim();
  if (!trimmed) {
    return null;
  }
  let obj: Record<string, unknown>;
  try {
    const parsed = JSON.parse(trimmed);
    if (!parsed || typeof parsed !== "object") {
      return null;
    }
    obj = parsed as Record<string, unknown>;
  } catch {
    return null;
  }

  const ts = typeof obj.ts === "string" ? shortTime(obj.ts) : null;
  const type = typeof obj.type === "string" ? obj.type : typeof obj.event_type === "string" ? obj.event_type : null;

  if (type === "system") {
    const subtype = typeof obj.subtype === "string" ? obj.subtype : "";
    return { ts, text: `[system] ${subtype}`.trimEnd() };
  }
  if (type === "result" || ("subtype" in obj && "num_turns" in obj)) {
    const subtype = stringField(obj, "subtype") ?? "?";
    const turns = obj.num_turns ?? "?";
    const stop = stringField(obj, "stop_reason") ?? "?";
    return { ts, text: `[result] subtype=${subtype} turns=${turns} stop_reason=${stop}` };
  }
  if (type === "assistant") {
    const text = renderAssistant(obj);
    return text ? { ts, text } : null;
  }
  if (type === "user") {
    const text = renderUser(obj);
    return text ? { ts, text } : null;
  }
  return null;
}

function renderAssistant(obj: Record<string, unknown>): string | null {
  const content = messageContent(obj);
  if (!content) {
    return null;
  }
  const parts: string[] = [];
  for (const block of content) {
    if (!block || typeof block !== "object") {
      continue;
    }
    const record = block as Record<string, unknown>;
    if (record.type === "text") {
      const snippet = clip(stringField(record, "text") ?? "", 160);
      if (snippet) {
        parts.push(`[assistant] ${snippet}`);
      }
    } else if (record.type === "tool_use") {
      parts.push(renderToolUse(record));
    }
  }
  return parts.length ? parts.join("\n") : null;
}

function renderToolUse(record: Record<string, unknown>): string {
  const name = stringField(record, "name") ?? "?";
  const input = record.input;
  if (!input || typeof input !== "object") {
    return `[tool_use ${name}]`;
  }
  const inp = input as Record<string, unknown>;
  if (name === "Bash") {
    return `[tool_use Bash] $ ${clip(stringField(inp, "command") ?? "", 160)}`;
  }
  if (name === "Read" || name === "Edit" || name === "Write") {
    return `[tool_use ${name}] ${stringField(inp, "file_path") ?? ""}`.trimEnd();
  }
  if (name === "Skill") {
    return `[tool_use Skill] /${stringField(inp, "skill") ?? "?"}`;
  }
  return `[tool_use ${name}]`;
}

function renderUser(obj: Record<string, unknown>): string | null {
  const content = messageContent(obj);
  if (!content) {
    return null;
  }
  const parts: string[] = [];
  for (const block of content) {
    if (!block || typeof block !== "object") {
      continue;
    }
    const record = block as Record<string, unknown>;
    if (record.type === "tool_result") {
      const body = record.content;
      let snippet = "";
      if (typeof body === "string") {
        snippet = body;
      } else if (Array.isArray(body)) {
        snippet = body
          .map((b) => (b && typeof b === "object" ? stringField(b as Record<string, unknown>, "text") ?? "" : String(b)))
          .join(" ");
      }
      parts.push(`[tool_result] ${clip(snippet.replace(/\n/g, " "), 120)}`);
    }
  }
  return parts.length ? parts.join("\n") : null;
}

function messageContent(obj: Record<string, unknown>): unknown[] | null {
  const message = obj.message;
  if (!message || typeof message !== "object") {
    return null;
  }
  const content = (message as Record<string, unknown>).content;
  return Array.isArray(content) ? content : null;
}

function stringField(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  return typeof value === "string" ? value : null;
}

function clip(value: string, max: number): string {
  const text = value.trim();
  return text.length <= max ? text : `${text.slice(0, max)}...`;
}

function shortTime(iso: string): string {
  const match = iso.match(/T(\d{2}:\d{2}:\d{2})/);
  return match ? match[1] : iso;
}
