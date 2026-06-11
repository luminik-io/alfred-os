import { describe, expect, it } from "vitest";

import { formatTranscriptLine } from "./transcript";

describe("formatTranscriptLine", () => {
  it("renders an assistant text block", () => {
    const line = formatTranscriptLine(
      JSON.stringify({
        type: "assistant",
        message: { role: "assistant", content: [{ type: "text", text: "Reading the code." }] },
      }),
    );
    expect(line).toEqual({ ts: null, text: "[assistant] Reading the code." });
  });

  it("renders a Bash tool_use with the command", () => {
    const line = formatTranscriptLine(
      JSON.stringify({
        type: "assistant",
        message: {
          role: "assistant",
          content: [{ type: "tool_use", name: "Bash", input: { command: "git status" } }],
        },
      }),
    );
    expect(line?.text).toBe("[tool_use Bash] $ git status");
  });

  it("renders a Read tool_use with the file path", () => {
    const line = formatTranscriptLine(
      JSON.stringify({
        type: "assistant",
        message: {
          role: "assistant",
          content: [{ type: "tool_use", name: "Read", input: { file_path: "/a/b.py" } }],
        },
      }),
    );
    expect(line?.text).toBe("[tool_use Read] /a/b.py");
  });

  it("renders a tool_result from a user turn", () => {
    const line = formatTranscriptLine(
      JSON.stringify({
        type: "user",
        message: { role: "user", content: [{ type: "tool_result", content: "file contents" }] },
      }),
    );
    expect(line?.text).toBe("[tool_result] file contents");
  });

  it("renders the final result event compactly", () => {
    const line = formatTranscriptLine(
      JSON.stringify({
        type: "result",
        subtype: "success",
        num_turns: 4,
        stop_reason: "end_turn",
      }),
    );
    expect(line?.text).toBe("[result] subtype=success turns=4 stop_reason=end_turn");
  });

  it("returns null for a torn / non-JSON line", () => {
    expect(formatTranscriptLine("not json {")).toBeNull();
    expect(formatTranscriptLine("   ")).toBeNull();
  });

  it("returns null for an empty assistant turn with nothing to show", () => {
    const line = formatTranscriptLine(
      JSON.stringify({ type: "assistant", message: { role: "assistant", content: [] } }),
    );
    expect(line).toBeNull();
  });
});
