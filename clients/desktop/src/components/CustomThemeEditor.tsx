import { useEffect, useMemo, useState } from "react";

import {
  type CustomRosterNames,
  editableAgents,
  EMPTY_CUSTOM_NAMES,
} from "../lib/agentThemes";
import { ROLE_LANE_LABEL, type WorkflowRole, WORKFLOW_ROLES } from "../lib/agentRoster";
import { Button } from "./ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { ScrollArea } from "./ui/scroll-area";

// The operator-authored custom roster editor: one row per known fleet agent,
// grouped by canonical role. The operator renames each agent's display name and
// (optionally) its role label; a blank field falls back to the shipped Batman
// name/role, so a half-filled custom theme is never blank. Saving persists the
// maps server-side (shared with the Slack path) via the parent's onSave, and
// also selects the `custom` theme so the change is visible immediately.
//
// Inputs are bounded and trimmed here for a tappable, forgiving UI; the server
// re-validates every entry (codename shape, length, control chars) on write, so
// this is a convenience layer, not the trust boundary.
const MAX_LABEL_LEN = 64;

export function CustomThemeEditor({
  open,
  value,
  onOpenChange,
  onSave,
}: {
  open: boolean;
  value: CustomRosterNames;
  onOpenChange: (open: boolean) => void;
  onSave: (next: CustomRosterNames) => void | Promise<void>;
}) {
  const agents = useMemo(() => editableAgents(), []);
  const [names, setNames] = useState<Record<string, string>>(value.names);
  const [roles, setRoles] = useState<Record<string, string>>(value.roles);
  const [saving, setSaving] = useState(false);

  // Re-seed the draft whenever the editor (re)opens with the persisted maps, so
  // a cancelled edit never leaks into the next open.
  useEffect(() => {
    if (open) {
      setNames({ ...value.names });
      setRoles({ ...value.roles });
    }
  }, [open, value.names, value.roles]);

  const setName = (codename: string, next: string) =>
    setNames((prev) => ({ ...prev, [codename]: next.slice(0, MAX_LABEL_LEN) }));
  const setRole = (codename: string, next: string) =>
    setRoles((prev) => ({ ...prev, [codename]: next.slice(0, MAX_LABEL_LEN) }));

  // Drop blank entries so the persisted map only carries real overrides.
  const clean = (map: Record<string, string>): Record<string, string> => {
    const out: Record<string, string> = {};
    for (const [codename, raw] of Object.entries(map)) {
      const trimmed = raw.trim();
      if (trimmed) out[codename] = trimmed;
    }
    return out;
  };

  const reset = () => {
    setNames({});
    setRoles({});
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      await onSave({ names: clean(names), roles: clean(roles) });
      onOpenChange(false);
    } finally {
      setSaving(false);
    }
  };

  const byRole = useMemo(() => {
    const groups = new Map<WorkflowRole, typeof agents>();
    for (const agent of agents) {
      const list = groups.get(agent.role) ?? [];
      list.push(agent);
      groups.set(agent.role, list);
    }
    return groups;
  }, [agents]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="custom-theme-editor max-w-2xl">
        <DialogHeader>
          <DialogTitle>Customize the roster</DialogTitle>
          <DialogDescription>
            Rename each agent and, optionally, its role label. Blank fields keep
            the default name. Your cast is shared with the desktop and the Slack
            messages the agents post.
          </DialogDescription>
        </DialogHeader>
        <ScrollArea className="custom-theme-editor__scroll max-h-[55vh] pr-3">
          <div className="custom-theme-editor__groups space-y-5">
            {WORKFLOW_ROLES.filter((role) => byRole.has(role)).map((role) => (
              <fieldset key={role} className="custom-theme-editor__group space-y-3">
                <legend className="custom-theme-editor__legend text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  {ROLE_LANE_LABEL[role]}
                </legend>
                {(byRole.get(role) ?? []).map((agent) => (
                  <div
                    key={agent.codename}
                    className="custom-theme-editor__row grid grid-cols-1 gap-2 sm:grid-cols-2"
                  >
                    <div className="space-y-1">
                      <Label
                        htmlFor={`ct-name-${agent.codename}`}
                        className="text-xs text-muted-foreground"
                      >
                        {agent.defaultName} — name
                      </Label>
                      <Input
                        id={`ct-name-${agent.codename}`}
                        value={names[agent.codename] ?? ""}
                        placeholder={agent.defaultName}
                        maxLength={MAX_LABEL_LEN}
                        onChange={(event) => setName(agent.codename, event.target.value)}
                      />
                    </div>
                    <div className="space-y-1">
                      <Label
                        htmlFor={`ct-role-${agent.codename}`}
                        className="text-xs text-muted-foreground"
                      >
                        Role label
                      </Label>
                      <Input
                        id={`ct-role-${agent.codename}`}
                        value={roles[agent.codename] ?? ""}
                        placeholder={agent.defaultRoleLabel}
                        maxLength={MAX_LABEL_LEN}
                        onChange={(event) => setRole(agent.codename, event.target.value)}
                      />
                    </div>
                  </div>
                ))}
              </fieldset>
            ))}
          </div>
        </ScrollArea>
        <DialogFooter className="custom-theme-editor__footer gap-2 sm:justify-between">
          <Button type="button" variant="ghost" size="sm" onClick={reset}>
            Reset all
          </Button>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => onOpenChange(false)}
              disabled={saving}
            >
              Cancel
            </Button>
            <Button type="button" size="sm" onClick={handleSave} disabled={saving}>
              {saving ? "Saving…" : "Save cast"}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { EMPTY_CUSTOM_NAMES };
