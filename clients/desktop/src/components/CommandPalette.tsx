import type { LucideIcon } from "lucide-react";

import {
  Command as CommandRoot,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandShortcut,
} from "./ui";

export type Command = {
  id: string;
  label: string;
  hint?: string;
  icon?: LucideIcon;
  run: () => void;
};

export function CommandPalette({
  open,
  onClose,
  commands,
}: {
  open: boolean;
  onClose: () => void;
  commands: Command[];
}) {
  const runCommand = (command: Command) => {
    command.run();
    onClose();
  };

  return (
    <CommandDialog
      open={open}
      onOpenChange={(next) => !next && onClose()}
      title="Command palette"
      description="Search Alfred actions."
      className="max-w-lg"
      showCloseButton
    >
      <CommandRoot>
        <CommandInput placeholder="Search actions" />
        <CommandList>
          <CommandEmpty>No actions found.</CommandEmpty>
          <CommandGroup heading="Actions">
            {commands.map((command) => {
              const Icon = command.icon;
              return (
                <CommandItem
                  key={command.id}
                  value={`${command.label} ${command.hint || ""}`}
                  onSelect={() => runCommand(command)}
                >
                  {Icon ? <Icon aria-hidden="true" /> : null}
                  <span>{command.label}</span>
                  {command.hint ? <CommandShortcut>{command.hint}</CommandShortcut> : null}
                </CommandItem>
              );
            })}
          </CommandGroup>
        </CommandList>
      </CommandRoot>
    </CommandDialog>
  );
}
