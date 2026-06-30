import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CustomThemeEditor } from "./CustomThemeEditor";

describe("CustomThemeEditor", () => {
  it("renders live custom agents beyond the shipped preset roster", () => {
    render(
      <CustomThemeEditor
        open
        value={{ names: {}, roles: {} }}
        agents={[
          {
            codename: "security-scout",
            role: "review",
            defaultName: "Sentinel",
            defaultRoleLabel: "Security reviewer",
          },
          {
            codename: "ops-sentinel",
            role: "ops",
            defaultName: "Ops Sentinel",
            defaultRoleLabel: "Ops & health",
          },
        ]}
        onOpenChange={vi.fn()}
        onSave={vi.fn()}
      />,
    );

    const nameInput = screen.getByLabelText("Sentinel name");
    expect(nameInput).toHaveAttribute("placeholder", "Sentinel");
    const row = nameInput.closest(".custom-theme-editor__row");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByLabelText("Role label")).toHaveAttribute(
      "placeholder",
      "Security reviewer",
    );
  });
});
