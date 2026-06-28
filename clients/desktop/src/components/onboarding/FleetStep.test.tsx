import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { EMPTY_CUSTOM_NAMES } from "../../lib/agentThemes";
import { FleetStep } from "./FleetStep";

describe("FleetStep", () => {
  it("keeps an open custom editor blocked while hydration is retrying", async () => {
    const onRetry = vi.fn();
    const onSaveCustom = vi.fn();
    const props = {
      value: "batman" as const,
      customNames: EMPTY_CUSTOM_NAMES,
      saveError: null,
      onChange: vi.fn(),
      onSaveCustom,
      onRetry,
    };
    const { rerender } = render(<FleetStep {...props} />);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /use custom names/i }));
    expect(screen.getByRole("dialog", { name: /customize the roster/i })).toBeInTheDocument();

    rerender(
      <FleetStep
        {...props}
        disabled
        blockedError="Wait for Alfred to load the saved fleet names before changing them."
      />,
    );

    const dialog = screen.getByRole("dialog", { name: /customize the roster/i });
    expect(within(dialog).getByText(/load the saved fleet names/i)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /save cast/i })).toBeDisabled();
    expect(within(dialog).getByLabelText(/Batman name/i)).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /^retry$/i }));

    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onSaveCustom).not.toHaveBeenCalled();
  });
});
