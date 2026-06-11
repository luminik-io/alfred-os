import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { KanbanBoard } from "./KanbanBoard";
import { parseIssueRef } from "../lib/links";
import type { ShippedBoard, ShippedCard } from "../types";

// Render in the desktop-capable mode so the queue actions appear.
vi.mock("../api", () => ({
  supportsNativeActions: () => true,
}));

function issue(overrides: Partial<ShippedCard> = {}): ShippedCard {
  return {
    repo: "your-org/api",
    number: 12,
    title: "Ready issue",
    url: "https://example.com/issues/12",
    author: "lucius",
    kind: "issue",
    timestamp: "2026-06-02T11:00:00Z",
    age_days: 0,
    is_draft: false,
    labels: [],
    ...overrides,
  };
}

function board(overrides: Partial<ShippedBoard> = {}): ShippedBoard {
  return {
    generated_at: "2026-06-02T12:00:00Z",
    lookback_days: 14,
    repos: ["your-org/api"],
    columns: {
      queued: [],
      in_progress: [],
      shipped: [],
    },
    counts: { queued: 0, in_progress: 0, shipped: 0 },
    errors: [],
    ...overrides,
  };
}

describe("KanbanBoard", () => {
  it("renders a load failure instead of false-empty columns", () => {
    render(<KanbanBoard board={null} state="error" error="/api/shipped timed out" />);

    expect(screen.getByText(/work failed to load/i)).toBeInTheDocument();
    expect(screen.getByText(/alfred could not load work/i)).toBeInTheDocument();
    expect(screen.queryByText(/no work is moving/i)).not.toBeInTheDocument();
  });

  it("renders server hard failures with empty columns as an error panel", () => {
    render(
      <KanbanBoard
        board={board({ error: "GitHub data unavailable for 3 watched repos" })}
        state="idle"
      />,
    );

    expect(screen.getByText(/couldn't reach github/i)).toBeInTheDocument();
    expect(screen.getByText(/work failed to build/i)).toBeInTheDocument();
    expect(screen.queryByText(/no work is moving/i)).not.toBeInTheDocument();
  });

  it("shows a useful clear-board workflow when all columns are empty", async () => {
    const onSwitch = vi.fn();
    const user = userEvent.setup();
    render(<KanbanBoard board={board()} state="idle" onSwitch={onSwitch} />);

    expect(screen.getByText(/no work is moving right now/i)).toBeInTheDocument();
    expect(screen.getByText(/no pickup-ready issues/i)).toBeInTheDocument();
    expect(screen.getByText(/no alfred PRs in flight/i)).toBeInTheDocument();
    expect(screen.getByText(/no alfred merges in the lookback/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /ask alfred/i })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /ask alfred/i }));
    expect(onSwitch).toHaveBeenCalledWith("compose");
  });

  it("keeps last good cards visible when a refresh fails", () => {
    render(
      <KanbanBoard
        board={board({
          columns: {
            queued: [
              {
                repo: "your-org/api",
                number: 12,
                title: "Ready issue",
                url: "https://example.com/issues/12",
                author: "lucius",
                kind: "issue",
                timestamp: "2026-06-02T11:00:00Z",
                age_days: 0,
                is_draft: false,
                labels: [],
              },
            ],
            in_progress: [],
            shipped: [],
          },
          counts: { queued: 1, in_progress: 0, shipped: 0 },
        })}
        state="error"
        error="network failed"
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText(/work refresh failed\. showing last update/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /ready issue/i })).toBeInTheDocument();
  });

  it("marks Batman large-feature issues as pickup-ready", () => {
    render(
      <KanbanBoard
        board={board({
          columns: {
            queued: [issue({ labels: ["agent:large-feature"] })],
            in_progress: [],
            shipped: [],
          },
          counts: { queued: 1, in_progress: 0, shipped: 0 },
        })}
        state="idle"
      />,
    );

    expect(screen.getByText(/pickup label/i)).toBeInTheDocument();
  });

  it("switches the active mobile lane tab", async () => {
    render(
      <KanbanBoard
        board={board({
          columns: {
            queued: [issue()],
            in_progress: [],
            shipped: [issue({ kind: "pr", number: 99, title: "Merged PR" })],
          },
          counts: { queued: 1, in_progress: 0, shipped: 1 },
        })}
        state="idle"
      />,
    );

    const user = userEvent.setup();
    expect(screen.getByRole("tab", { name: /ready/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await user.click(screen.getByRole("tab", { name: /shipped/i }));
    expect(screen.getByRole("tab", { name: /shipped/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("holds a queued issue from its card action", async () => {
    const onQueueAction = vi.fn();
    render(
      <KanbanBoard
        board={board({
          columns: { queued: [issue()], in_progress: [], shipped: [] },
          counts: { queued: 1, in_progress: 0, shipped: 0 },
        })}
        state="idle"
        onQueueAction={onQueueAction}
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /hold/i }));
    expect(onQueueAction).toHaveBeenCalledWith("your-org/api", 12, "hold");
  });

  it("closes a queued issue as done from its card action", async () => {
    const onQueueAction = vi.fn();
    render(
      <KanbanBoard
        board={board({
          columns: { queued: [issue()], in_progress: [], shipped: [] },
          counts: { queued: 1, in_progress: 0, shipped: 0 },
        })}
        state="idle"
        onQueueAction={onQueueAction}
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /^done$/i }));
    expect(onQueueAction).toHaveBeenCalledWith("your-org/api", 12, "done");
  });

  it("does not offer Done or Hold on PRs (only queued issues are queue-actionable)", () => {
    render(
      <KanbanBoard
        board={board({
          columns: {
            queued: [],
            in_progress: [issue({ kind: "pr", number: 99, title: "Open PR" })],
            shipped: [],
          },
          counts: { queued: 0, in_progress: 1, shipped: 0 },
        })}
        state="idle"
        onQueueAction={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /^done$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /hold/i })).not.toBeInTheDocument();
  });

  it("does not offer Done or Hold on demo cards", () => {
    render(
      <KanbanBoard
        board={board({
          columns: {
            queued: [
              issue({
                repo: "alfred/demo",
                title: "[Demo] Try the board",
                url: null,
                demo: true,
              }),
            ],
            in_progress: [],
            shipped: [],
          },
          counts: { queued: 1, in_progress: 0, shipped: 0 },
        })}
        state="idle"
        onQueueAction={vi.fn()}
      />,
    );
    expect(screen.getByText(/\[demo\] try the board/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^done$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /hold/i })).not.toBeInTheDocument();
  });

  it("does not offer Hold on PRs (only queued issues are queue-actionable)", () => {
    render(
      <KanbanBoard
        board={board({
          columns: {
            queued: [],
            in_progress: [issue({ kind: "pr", number: 99, title: "Open PR" })],
            shipped: [],
          },
          counts: { queued: 0, in_progress: 1, shipped: 0 },
        })}
        state="idle"
        onQueueAction={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /hold/i })).not.toBeInTheDocument();
  });

  it("assigns an issue typed into the composer", async () => {
    const onQueueAction = vi.fn();
    render(<KanbanBoard board={board()} state="idle" onQueueAction={onQueueAction} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/assign an issue/i), "your-org/api#42");
    await user.click(screen.getByRole("button", { name: /^assign$/i }));
    expect(onQueueAction).toHaveBeenCalledWith("your-org/api", 42, "assign");
  });

  it("keeps the typed ref in the composer when assignment fails", async () => {
    const onQueueAction = vi.fn().mockResolvedValue(false);
    render(<KanbanBoard board={board()} state="idle" onQueueAction={onQueueAction} />);
    const user = userEvent.setup();
    const input = screen.getByLabelText(/assign an issue/i);
    await user.type(input, "your-org/api#42");
    await user.click(screen.getByRole("button", { name: /^assign$/i }));
    expect(onQueueAction).toHaveBeenCalledWith("your-org/api", 42, "assign");
    // A failed POST must not silently discard what the operator typed.
    expect(input).toHaveValue("your-org/api#42");
  });

  it("surfaces a queue action error on the board", () => {
    render(
      <KanbanBoard
        board={board()}
        state="idle"
        onQueueAction={vi.fn()}
        notice={{ tone: "error", message: "forbidden", domain: "board" }}
      />,
    );
    expect(screen.getByText(/forbidden/i)).toBeInTheDocument();
  });
});

describe("parseIssueRef", () => {
  it("parses owner/repo#number", () => {
    expect(parseIssueRef("your-org/api#42")).toEqual({ repo: "your-org/api", number: 42 });
  });
  it("parses a GitHub issue URL", () => {
    expect(parseIssueRef("https://github.com/your-org/api/issues/7")).toEqual({
      repo: "your-org/api",
      number: 7,
    });
  });
  it("rejects malformed refs", () => {
    expect(parseIssueRef("not a ref")).toBeNull();
    expect(parseIssueRef("your-org/api")).toBeNull();
    expect(parseIssueRef("https://github.com/your-org/api/pull/8")).toBeNull();
    expect(parseIssueRef("#12")).toBeNull();
  });
});
