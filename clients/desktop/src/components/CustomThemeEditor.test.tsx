import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { EMPTY_CUSTOM_NAMES } from "../lib/agentThemes";
import { CustomThemeEditor } from "./CustomThemeEditor";

describe("CustomThemeEditor", () => {
  it("blocks custom roster saves and exposes retry while hydration failed", async () => {
    const onOpenChange = vi.fn();
    const onSave = vi.fn();
    const onRetryBlocked = vi.fn();
    render(
      <CustomThemeEditor
        open
        value={EMPTY_CUSTOM_NAMES}
        blockedError="Could not load saved fleet names from Alfred."
        onOpenChange={onOpenChange}
        onSave={onSave}
        onRetryBlocked={onRetryBlocked}
      />,
    );
    const user = userEvent.setup();

    expect(screen.getByRole("button", { name: /save cast/i })).toBeDisabled();
    expect(screen.getByLabelText(/Batman name/i)).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /^retry$/i }));

    expect(onRetryBlocked).toHaveBeenCalledTimes(1);
    expect(onSave).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalled();
  });

  it("keeps the draft open when parent values change after a failed save", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const onSave = vi.fn(async () => false);
    const { rerender } = render(
      <CustomThemeEditor
        open
        value={{ names: { batman: "Original" }, roles: {} }}
        onOpenChange={onOpenChange}
        onSave={onSave}
      />,
    );
    const nameInput = screen.getByLabelText(/Batman name/i);
    await user.clear(nameInput);
    await user.type(nameInput, "Draft Wayne");

    await user.click(screen.getByRole("button", { name: /save cast/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not save custom names/i);

    rerender(
      <CustomThemeEditor
        open
        value={{ names: { batman: "Server Wayne" }, roles: {} }}
        onOpenChange={onOpenChange}
        onSave={onSave}
      />,
    );

    expect(screen.getByLabelText(/Batman name/i)).toHaveValue("Draft Wayne");
    expect(screen.getByRole("alert")).toHaveTextContent(/could not save custom names/i);
    expect(onOpenChange).not.toHaveBeenCalled();
  });
});
