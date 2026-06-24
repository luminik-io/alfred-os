import { Clock3, Pause, Play, RotateCw, ScrollText, Square } from "lucide-react";
import { useEffect, useState } from "react";

import { exactTime, friendlyTime } from "../format";
import type { FleetControlRow } from "../lib/fleetControl";
import type { NativeAction, ScheduledRun } from "../types";
import { AlfredMetric, AlfredStatusDot } from "./ui/alfred";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Label } from "./ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "./ui/sheet";

/**
 * The agent detail slide-over. A non-modal right sheet that overlays the canvas
 * on node/row select and is dismissible (X, Escape, or click-away), so the
 * canvas keeps the real estate it used to share with a permanent panel.
 */
export function AgentDetailDrawer({
  row,
  open,
  onOpenChange,
  schedule,
  canRun,
  nativeBusy,
  serviceTone,
  agentProfile,
  agentActionCue,
  scheduleCopy,
  editableScheduleValue,
  scheduleOptions,
  onDispatch,
  onViewLogs,
}: {
  row: FleetControlRow | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  schedule?: ScheduledRun;
  canRun: boolean;
  nativeBusy: string | null;
  serviceTone: (row: FleetControlRow) => {
    tone: "ok" | "warn" | "error" | "idle";
    label: string;
  };
  agentProfile: (
    row: FleetControlRow,
    schedule?: ScheduledRun,
  ) => { label: string; purpose: string; themeAccent: string };
  agentActionCue: (row: FleetControlRow) => string;
  scheduleCopy: (row: FleetControlRow, schedule?: ScheduledRun) => string;
  editableScheduleValue: (schedule?: ScheduledRun) => string;
  scheduleOptions: (current: string) => Array<{ value: string; label: string }>;
  onDispatch: (
    action: NativeAction,
    codename: string,
    label: string,
    cadence?: string,
  ) => void;
  onViewLogs: (codename: string) => void;
}) {
  return (
    <Sheet open={open && Boolean(row)} onOpenChange={onOpenChange} modal={false}>
      <SheetContent
        side="right"
        className="agent-drawer"
        // Non-modal: the canvas behind stays interactive, so we do not steal
        // focus on open and we do not trap pointer events outside the panel.
        onOpenAutoFocus={(event) => event.preventDefault()}
        onInteractOutside={(event) => {
          // Clicking a different node should reselect, not close mid-gesture.
          const target = event.target as HTMLElement | null;
          if (target?.closest(".workflow-graph")) {
            event.preventDefault();
          }
        }}
        aria-label={row ? `${agentProfile(row, schedule).label} details` : "Agent details"}
      >
        {row ? (
          <DrawerBody
            row={row}
            schedule={schedule}
            canRun={canRun}
            nativeBusy={nativeBusy}
            serviceTone={serviceTone}
            agentProfile={agentProfile}
            agentActionCue={agentActionCue}
            scheduleCopy={scheduleCopy}
            editableScheduleValue={editableScheduleValue}
            scheduleOptions={scheduleOptions}
            onDispatch={onDispatch}
            onViewLogs={onViewLogs}
          />
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

function DrawerBody({
  row,
  schedule,
  canRun,
  nativeBusy,
  serviceTone,
  agentProfile,
  agentActionCue,
  scheduleCopy,
  editableScheduleValue,
  scheduleOptions,
  onDispatch,
  onViewLogs,
}: {
  row: FleetControlRow;
  schedule?: ScheduledRun;
  canRun: boolean;
  nativeBusy: string | null;
  serviceTone: (row: FleetControlRow) => {
    tone: "ok" | "warn" | "error" | "idle";
    label: string;
  };
  agentProfile: (
    row: FleetControlRow,
    schedule?: ScheduledRun,
  ) => { label: string; purpose: string; themeAccent: string };
  agentActionCue: (row: FleetControlRow) => string;
  scheduleCopy: (row: FleetControlRow, schedule?: ScheduledRun) => string;
  editableScheduleValue: (schedule?: ScheduledRun) => string;
  scheduleOptions: (current: string) => Array<{ value: string; label: string }>;
  onDispatch: (
    action: NativeAction,
    codename: string,
    label: string,
    cadence?: string,
  ) => void;
  onViewLogs: (codename: string) => void;
}) {
  const busy = (action: NativeAction) => nativeBusy === `${action}:${row.codename}`;
  const currentCadence = editableScheduleValue(schedule);
  const [draftCadence, setDraftCadence] = useState(currentCadence);

  useEffect(() => {
    setDraftCadence(currentCadence);
  }, [currentCadence]);

  const options = scheduleOptions(currentCadence);
  const profile = agentProfile(row, schedule);
  const { tone, label } = serviceTone(row);
  const monogram =
    profile.label.replace(/\s*·.*$/, "").trim().charAt(0).toUpperCase() || "A";

  return (
    <div
      className="agent-drawer__inner"
      style={{ "--agent-accent": profile.themeAccent } as React.CSSProperties}
    >
      <SheetHeader className="agent-drawer__header">
        <div className="agent-drawer__identity">
          <span className="agent-drawer__pulse" aria-hidden="true">
            {monogram}
          </span>
          <div className="min-w-0">
            <div className="mb-1.5 flex min-w-0 flex-wrap items-center gap-2">
              <Badge
                variant="secondary"
                className="h-5 border-border/50 bg-secondary/65 px-2 text-[0.68rem]"
                title={`Runtime codename: ${row.codename}`}
              >
                {row.codename}
              </Badge>
              <Badge
                variant="outline"
                className="alfred-status-badge"
                data-tone={tone}
                aria-label={`Status: ${label}`}
              >
                <AlfredStatusDot tone={tone} aria-hidden="true" />
                {label}
              </Badge>
            </div>
            <SheetTitle className="agent-drawer__title">{profile.label}</SheetTitle>
          </div>
        </div>
        <SheetDescription className="agent-drawer__purpose">
          {profile.purpose || agentActionCue(row)}
        </SheetDescription>
      </SheetHeader>

      <div className="agent-drawer__body">
        <dl className="agent-drawer__metrics">
          <MetaItem label="Last run" title={exactTime(row.summary?.last_run_at)}>
            {row.summary ? friendlyTime(row.summary.last_run_at) : "No run"}
          </MetaItem>
          <MetaItem label="Runs today">{row.summary?.firings_today ?? 0}</MetaItem>
          <MetaItem label="Fail streak">{row.consecutiveFailures}</MetaItem>
          <MetaItem label="Schedule">{scheduleCopy(row, schedule)}</MetaItem>
        </dl>

        <div className="agent-drawer__latest">
          <span>Latest signal</span>
          <p>
            {row.paused && row.pausedSince
              ? `Paused since ${friendlyTime(row.pausedSince)}.`
              : row.summary?.last_summary || "No runs yet."}
          </p>
        </div>

        <div className="agent-drawer__actions">
          <Button type="button" variant="outline" onClick={() => onViewLogs(row.codename)}>
            <ScrollText aria-hidden="true" />
            Logs
          </Button>
          {canRun && row.paused ? (
            <Button
              type="button"
              variant="default"
              disabled={busy("resume")}
              aria-busy={busy("resume")}
              onClick={() => onDispatch("resume", row.codename, "Resume")}
            >
              <Play aria-hidden="true" />
              {busy("resume") ? "Resuming" : "Resume"}
            </Button>
          ) : null}
          {canRun && !row.paused ? (
            <Button
              type="button"
              variant="outline"
              disabled={busy("pause")}
              aria-busy={busy("pause")}
              onClick={() => onDispatch("pause", row.codename, "Pause")}
            >
              <Pause aria-hidden="true" />
              {busy("pause") ? "Pausing" : "Pause"}
            </Button>
          ) : null}
          {canRun ? (
            <>
              <Button
                type="button"
                variant="secondary"
                disabled={busy("run")}
                aria-busy={busy("run")}
                onClick={() => onDispatch("run", row.codename, "Run")}
              >
                <RotateCw aria-hidden="true" />
                {busy("run") ? "Running" : "Run once"}
              </Button>
              <Button
                type="button"
                variant="secondary"
                disabled={busy("dry_run")}
                aria-busy={busy("dry_run")}
                onClick={() => onDispatch("dry_run", row.codename, "Dry-run")}
              >
                <Square aria-hidden="true" />
                {busy("dry_run") ? "Running" : "Dry-run"}
              </Button>
            </>
          ) : null}
        </div>

        {canRun ? (
          <div className="agent-drawer__schedule">
            <div className="min-w-0 space-y-1.5">
              <Label
                htmlFor={`drawer-schedule-${row.codename}`}
                className="flex items-center gap-1.5 text-xs text-muted-foreground"
              >
                <Clock3 className="size-3.5" aria-hidden="true" />
                Cadence
              </Label>
              <Select value={draftCadence} onValueChange={setDraftCadence}>
                <SelectTrigger
                  id={`drawer-schedule-${row.codename}`}
                  aria-label={`Schedule ${row.codename}`}
                  className="w-full"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {options.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button
              type="button"
              variant="outline"
              aria-label={`Set ${row.codename} schedule`}
              disabled={busy("schedule") || !draftCadence.trim()}
              aria-busy={busy("schedule")}
              onClick={() => onDispatch("schedule", row.codename, "Set schedule", draftCadence)}
            >
              {busy("schedule") ? "Setting" : "Set"}
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function MetaItem({
  children,
  label,
  title,
}: {
  children: React.ReactNode;
  label: string;
  title?: string;
}) {
  return (
    <AlfredMetric
      asDescription
      className="min-w-0"
      label={label}
      title={title}
      value={children}
    />
  );
}
