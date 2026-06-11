import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RequestThread } from "./RequestThread";
import type { RequestThreadModel } from "../lib/uiTypes";

vi.mock("../lib/links", () => ({
  openExternal: vi.fn(),
}));

function thread(overrides: Partial<RequestThreadModel> = {}): RequestThreadModel {
  return {
    id: "your-org/api#12",
    title: "Add CSV export",
    repo: "your-org/api",
    repos: ["your-org/api"],
    issueNumber: 12,
    url: "https://github.com/your-org/api/issues/12",
    steps: [
      { key: "intake", label: "Intake", state: "done" },
      { key: "plan", label: "Plan", state: "active" },
      { key: "queued", label: "Queued", state: "pending" },
      { key: "building", label: "Building", state: "pending" },
      { key: "shipped", label: "Shipped", state: "pending" },
    ],
    correlationApproximate: true,
    ...overrides,
  };
}

describe("RequestThread", () => {
  it("renders every lifecycle step", () => {
    render(<RequestThread thread={thread()} />);
    for (const label of ["Intake", "Plan", "Queued", "Building", "Shipped"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("offers a plan sign-off when the plan step is active", async () => {
    const onOpenPlan = vi.fn();
    render(<RequestThread thread={thread()} onOpenPlan={onOpenPlan} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /review plan/i }));
    expect(onOpenPlan).toHaveBeenCalledTimes(1);
  });

  it("does not offer the plan sign-off when the plan step is not active", () => {
    render(
      <RequestThread
        thread={thread({
          steps: [
            { key: "intake", label: "Intake", state: "done" },
            { key: "plan", label: "Plan", state: "missing" },
            { key: "queued", label: "Queued", state: "done" },
            { key: "building", label: "Building", state: "active" },
            { key: "shipped", label: "Shipped", state: "pending" },
          ],
        })}
        onOpenPlan={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /review plan/i })).not.toBeInTheDocument();
  });

  it("flags an approximate correlation honestly", () => {
    render(<RequestThread thread={thread()} />);
    expect(screen.getByText(/github evidence/i)).toBeInTheDocument();
    expect(screen.getByText(/no guessing/i)).toBeInTheDocument();
  });

  it("marks a missing step as waiting for evidence", () => {
    render(
      <RequestThread
        thread={thread({
          steps: [
            { key: "intake", label: "Intake", state: "done" },
            { key: "plan", label: "Plan", state: "missing" },
            { key: "queued", label: "Queued", state: "pending" },
            { key: "building", label: "Building", state: "pending" },
            { key: "shipped", label: "Shipped", state: "pending" },
          ],
        })}
      />,
    );
    expect(screen.getByText(/· waiting for evidence/i)).toBeInTheDocument();
  });

  it("summarizes multi-repo plan context without asking for repo input", () => {
    render(
      <RequestThread
        thread={thread({
          repo: "your-org/api",
          repos: ["your-org/api", "your-org/web", "your-org/specs"],
          issueNumber: null,
        })}
      />,
    );
    const context = screen.getByText(/3 codebases in scope/i);
    expect(context).toBeInTheDocument();
    expect(context).toHaveAttribute("title", "your-org/api, your-org/web, your-org/specs");
  });
});
