import {
  Clock3,
  Pause,
  Play,
  Rows3,
  RotateCw,
  ScrollText,
  Square,
  Workflow as WorkflowIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { supportsNativeActions } from "../api";
import { exactTime, friendlyTime, titleCase } from "../format";
import { isErrorStatus } from "../lib/derive";
import {
  buildFleetRows,
  deriveFleetHealth,
  type FleetControlRow,
  type FleetServiceState,
} from "../lib/fleetControl";
import type { NativeActionRequest } from "../lib/uiTypes";
import { WORKFLOW_AGENTS, type WorkflowNodeInput } from "../lib/workflowGraph";
import type { AgentSummary, NativeAction, ScheduledRun } from "../types";
import { WorkflowGraph } from "./WorkflowGraph";
import { AlfredMetric, AlfredStatusDot, type AlfredTone } from "./ui/alfred";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { CardDescription, CardHeader, CardTitle } from "./ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Label } from "./ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select";

const CONFIRM_VERBS: ReadonlySet<NativeAction> = new Set(["pause", "resume", "run"]);
const SCHEDULE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "10m", label: "Every 10 min" },
  { value: "20m", label: "Every 20 min" },
  { value: "30m", label: "Every 30 min" },
  { value: "1h", label: "Hourly" },
  { value: "2h", label: "Every 2 hours" },
  { value: "daily@09:00", label: "Daily 09:00" },
  { value: "weekly@mon:09:00", label: "Monday 09:00" },
];

type PendingAction = { action: NativeAction; codename: string; label: string } | null;

type RosterView = "workflow" | "list";
const ROSTER_VIEW_KEY = "alfred.rosterView";

function readRosterView(): RosterView {
  try {
    return window.localStorage.getItem(ROSTER_VIEW_KEY) === "list"
      ? "list"
      : "workflow";
  } catch {
    return "workflow";
  }
}

export function FleetControlView({
  agents,
  schedule,
  service,
  nativeBusy,
  onRunLocalAction,
  onViewLogs,
}: {
  agents: AgentSummary[];
  schedule?: ScheduledRun[];
  service: FleetServiceState;
  nativeBusy: string | null;
  onRunLocalAction: (request: NativeActionRequest) => void;
  onViewLogs: (codename: string) => void;
}) {
  const canRun = supportsNativeActions();
  const rows = buildFleetRows(agents, service);
  const scheduleByCodename = useMemo(() => scheduleMap(schedule || []), [schedule]);
  const health = deriveFleetHealth(rows);
  const stats = agentStats(rows);
  const defaultSelected = defaultSelectedCodename(rows);
  const [selectedCodename, setSelectedCodename] = useState<string | null>(defaultSelected);
  const [pending, setPending] = useState<PendingAction>(null);
  const [viewMode, setViewMode] = useState<RosterView>(() => readRosterView());
  const affirmRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    try {
      window.localStorage.setItem(ROSTER_VIEW_KEY, viewMode);
    } catch {
      // Private mode / no storage: keep the choice in memory only.
    }
  }, [viewMode]);
  const selectedRow = rows.find((row) => row.codename === selectedCodename) || rows[0] || null;
  const selectedSchedule = selectedRow
    ? scheduleFor(scheduleByCodename, selectedRow.codename)
    : undefined;

  // Live display data for the pipeline agents, derived with the same helpers
  // the cards use so status/accent/runs stay consistent across views.
  const workflowInputs = useMemo<WorkflowNodeInput[]>(
    () =>
      rows
        .filter((row) => WORKFLOW_AGENTS.includes(row.codename))
        .map((row) => {
          const profile = agentProfile(row, scheduleFor(scheduleByCodename, row.codename));
          const { tone, label } = serviceTone(row);
          return {
            codename: row.codename,
            label: profile.label,
            role: row.summary?.role_title || titleCase(row.codename),
            accent: profile.themeAccent,
            tone,
            statusLabel: label,
            runsToday: row.summary?.firings_today ?? 0,
          };
        }),
    [rows, scheduleByCodename],
  );

  useEffect(() => {
    if (!rows.length) {
      setSelectedCodename(null);
      return;
    }
    if (!selectedCodename || !rows.some((row) => row.codename === selectedCodename)) {
      setSelectedCodename(defaultSelectedCodename(rows));
    }
  }, [rows, selectedCodename]);

  const dispatch = (
    action: NativeAction,
    codename: string,
    label: string,
    cadence?: string,
  ) => {
    if (CONFIRM_VERBS.has(action)) {
      setPending({ action, codename, label });
      return;
    }
    onRunLocalAction({ action, target: codename, cadence, refreshAfter: true });
  };

  const confirm = () => {
    if (!pending) return;
    onRunLocalAction({ action: pending.action, target: pending.codename, refreshAfter: true });
    setPending(null);
  };

  return (
    <section className="agents-deck" aria-label="Agent roster">
      {rows.length ? (
        <>
          <header className="agents-deck__command">
            <div className="agents-deck__summary">
              <SummaryStat
                label="Health"
                value={fleetHealthLabel(health.level)}
                tone={health.level}
              />
              <SummaryStat
                label="Running"
                value={stats.running}
                tone={stats.running ? "ok" : "idle"}
              />
              <SummaryStat
                label="Paused"
                value={stats.paused + stats.stopped}
                tone={stats.paused || stats.stopped ? "warn" : "ok"}
              />
              <SummaryStat
                label="Failing"
                value={stats.erroring}
                tone={stats.erroring ? "error" : "ok"}
              />
            </div>
            <div
              className="agents-deck__viewtoggle"
              role="group"
              aria-label="Roster view"
            >
              <button
                type="button"
                data-active={viewMode === "workflow" ? "true" : "false"}
                aria-pressed={viewMode === "workflow"}
                aria-label="Workflow view"
                onClick={() => setViewMode("workflow")}
              >
                <WorkflowIcon aria-hidden="true" />
                <span>Workflow</span>
              </button>
              <button
                type="button"
                data-active={viewMode === "list" ? "true" : "false"}
                aria-pressed={viewMode === "list"}
                aria-label="List view"
                onClick={() => setViewMode("list")}
              >
                <Rows3 aria-hidden="true" />
                <span>List</span>
              </button>
            </div>
          </header>

          <div
            className={
              viewMode === "workflow" ? "agents-deck__stage" : "agents-deck__grid"
            }
          >
            {viewMode === "workflow" ? (
              <WorkflowGraph
                agents={workflowInputs}
                selectedCodename={selectedRow?.codename ?? null}
                onSelect={setSelectedCodename}
              />
            ) : (
              <div className="agents-deck__rail" aria-label="Roster list">
                <div
                  className="agents-deck__list motion-rise"
                  role="group"
                  aria-label="Agents"
                >
                  {rows.map((row) => (
                    <AgentRosterRow
                      key={row.codename}
                      row={row}
                      schedule={scheduleFor(scheduleByCodename, row.codename)}
                      selected={row.codename === selectedRow?.codename}
                      onSelect={() => setSelectedCodename(row.codename)}
                    />
                  ))}
                </div>
              </div>
            )}
            {selectedRow ? (
              <AgentInspector
                row={selectedRow}
                schedule={selectedSchedule}
                canRun={canRun}
                nativeBusy={nativeBusy}
                onDispatch={dispatch}
                onViewLogs={onViewLogs}
              />
            ) : null}
          </div>
        </>
      ) : (
        <div className="agents-deck__empty">
          <CardHeader>
            <CardTitle>No agents detected</CardTitle>
            <CardDescription>Connect to Alfred serve to load the roster.</CardDescription>
          </CardHeader>
        </div>
      )}

      <Dialog open={Boolean(pending)} onOpenChange={(open) => !open && setPending(null)}>
        <DialogContent
          role="alertdialog"
          showCloseButton={false}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            affirmRef.current?.focus();
          }}
        >
          <DialogHeader>
            <DialogTitle>{pending ? `${pending.label} ${pending.codename}` : "Confirm action"}</DialogTitle>
            <DialogDescription>
              {pending ? (
                <>
                  {confirmCopy(pending.action)} <strong>{pending.codename}</strong>.
                </>
              ) : (
                "Confirm the fleet action."
              )}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setPending(null)}>
              Cancel
            </Button>
            <Button type="button" variant="destructive" onClick={confirm} ref={affirmRef}>
              Yes, {pending?.label.toLowerCase() || "confirm"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

function AgentRosterRow({
  onSelect,
  row,
  schedule,
  selected,
}: {
  onSelect: () => void;
  row: FleetControlRow;
  schedule?: ScheduledRun;
  selected: boolean;
}) {
  const profile = agentProfile(row, schedule);
  const { tone, label } = serviceTone(row);
  return (
    <button
      type="button"
      className="agents-deck__row"
      data-selected={selected ? "true" : "false"}
      style={{ "--agent-accent": profile.themeAccent } as React.CSSProperties}
      onClick={onSelect}
      aria-current={selected ? "true" : undefined}
      aria-label={`Select ${profile.label}`}
    >
      <span className="agents-deck__row-mark" aria-hidden="true" />
      <span className="min-w-0">
        <span className="agents-deck__row-title">{profile.label}</span>
        <span className="agents-deck__row-purpose">
          {profile.purpose || scheduleCopy(row, schedule)}
        </span>
      </span>
      <span className="agents-deck__row-meta">
        <Badge
          variant="outline"
          className="alfred-status-badge"
          data-tone={tone}
          aria-label={`Status: ${label}`}
        >
          <AlfredStatusDot tone={tone} aria-hidden="true" />
          {label}
        </Badge>
        <span>{scheduleCopy(row, schedule)}</span>
      </span>
    </button>
  );
}

function AgentInspector({
  row,
  schedule,
  canRun,
  nativeBusy,
  onDispatch,
  onViewLogs,
}: {
  row: FleetControlRow;
  schedule?: ScheduledRun;
  canRun: boolean;
  nativeBusy: string | null;
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
  // Agent monogram: the first letter of the display name in the accent color,
  // a small identity mark instead of an empty decorative chip.
  const monogram =
    profile.label.replace(/\s*·.*$/, "").trim().charAt(0).toUpperCase() || "A";
  return (
    <section
      className="agent-inspector"
      aria-label={`${profile.label} details`}
      style={{ "--agent-accent": profile.themeAccent } as React.CSSProperties}
    >
      <div className="agent-inspector__header">
        <div className="min-w-0">
          <div className="mb-2 flex min-w-0 flex-wrap items-center gap-2">
            <Badge
              variant="secondary"
              className="h-5 border-border/50 bg-secondary/65 px-2 text-[0.68rem]"
              title={`Runtime codename: ${row.codename}`}
            >
              {row.codename}
            </Badge>
            <ServiceBadge row={row} />
          </div>
          <h2 className="agent-inspector__title">{profile.label}</h2>
          <p className="agent-inspector__purpose">
            {profile.purpose || agentActionCue(row)}
          </p>
        </div>
        <div className="agent-inspector__pulse" aria-hidden="true">
          {monogram}
        </div>
      </div>

      <div className="agent-inspector__body">
        <dl className="agent-inspector__metrics">
          <MetaItem label="Last run" title={exactTime(row.summary?.last_run_at)}>
            {row.summary ? friendlyTime(row.summary.last_run_at) : "No run"}
          </MetaItem>
          <MetaItem label="Runs today">{row.summary?.firings_today ?? 0}</MetaItem>
          <MetaItem label="Fail streak">{row.consecutiveFailures}</MetaItem>
          <MetaItem label="Schedule">{scheduleCopy(row, schedule)}</MetaItem>
        </dl>

        <div className="agent-inspector__latest">
          <span>Latest signal</span>
          <p>
            {row.paused && row.pausedSince
              ? `Paused since ${friendlyTime(row.pausedSince)}.`
              : row.summary?.last_summary || "No runs yet."}
          </p>
        </div>

        <div className="agent-inspector__actions">
          <Button type="button" variant="outline" onClick={() => onViewLogs(row.codename)}>
            <ScrollText aria-hidden="true" />
            Logs
          </Button>
          {canRun && row.paused ? (
            <Button
              type="button"
              variant="default"
              disabled={busy("resume")}
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
                onClick={() => onDispatch("run", row.codename, "Run")}
              >
                <RotateCw aria-hidden="true" />
                {busy("run") ? "Running" : "Run once"}
              </Button>
              <Button
                type="button"
                variant="secondary"
                disabled={busy("dry_run")}
                onClick={() => onDispatch("dry_run", row.codename, "Dry-run")}
              >
                <Square aria-hidden="true" />
                {busy("dry_run") ? "Running" : "Dry-run"}
              </Button>
            </>
          ) : null}
        </div>

        {canRun ? (
          <div className="agent-inspector__schedule">
            <div className="min-w-0 space-y-1.5">
              <Label
                htmlFor={`schedule-${row.codename}`}
                className="flex items-center gap-1.5 text-xs text-muted-foreground"
              >
                <Clock3 className="size-3.5" aria-hidden="true" />
                Cadence
              </Label>
              <Select value={draftCadence} onValueChange={setDraftCadence}>
                <SelectTrigger
                  id={`schedule-${row.codename}`}
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
              onClick={() => onDispatch("schedule", row.codename, "Set schedule", draftCadence)}
            >
              {busy("schedule") ? "Setting" : "Set"}
            </Button>
          </div>
        ) : null}
      </div>
    </section>
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

function ServiceBadge({ row }: { row: FleetControlRow }) {
  const { tone, label } = serviceTone(row);
  return (
    <Badge
      variant="outline"
      className="alfred-status-badge"
      data-tone={tone}
      aria-label={`Status: ${label}`}
    >
      <AlfredStatusDot tone={tone} aria-hidden="true" />
      {label}
    </Badge>
  );
}

function serviceTone(row: FleetControlRow): {
  tone: "ok" | "warn" | "error" | "idle";
  label: string;
} {
  // Honest human vocabulary (DESIGN_SPEC chip map): an errored run reads as a
  // snag, a live agent is "Working now", and an idle agent is "Resting" or
  // "Paused" with a neutral tone. idle never shows the green/ok tone.
  if (isErrorStatus(row.summary?.status) || row.consecutiveFailures >= 2) {
    return { tone: "error", label: "Hit a snag" };
  }
  if (row.service === "paused" || row.service === "stopped") {
    return { tone: "idle", label: "Paused" };
  }
  // Only a genuinely live run shows the working tone. A loaded-but-idle agent
  // is scheduled, not executing, so it reads as "Resting" with a neutral tone,
  // never green.
  if (row.summary?.status === "live") {
    return { tone: "ok", label: "Working now" };
  }
  return { tone: "idle", label: "Resting" };
}

function defaultSelectedCodename(rows: FleetControlRow[]): string | null {
  return (
    rows.find((row) => isErrorStatus(row.summary?.status) || row.consecutiveFailures >= 2)
      ?.codename ||
    rows.find((row) => row.service === "running")?.codename ||
    rows[0]?.codename ||
    null
  );
}

function fleetHealthLabel(level: "ok" | "warn" | "error" | "unknown"): string {
  if (level === "ok") return "Live";
  if (level === "warn") return "Needs attention";
  if (level === "error") return "Failing";
  return "Unknown";
}

function agentProfile(row: FleetControlRow, schedule?: ScheduledRun): {
  label: string;
  purpose: string;
  themeAccent: string;
} {
  const displayName = row.summary?.display_name || schedule?.display_name || titleCase(row.codename);
  const roleTitle = row.summary?.role_title || schedule?.role_title || schedule?.role || "";
  const purpose = row.summary?.purpose || schedule?.purpose || "";
  return {
    label: roleTitle ? `${displayName} · ${roleTitle}` : displayName,
    purpose,
    themeAccent: row.summary?.theme_accent || schedule?.theme_accent || "var(--primary)",
  };
}

function confirmCopy(action: NativeAction): string {
  if (action === "pause") return "Pause scheduled runs for";
  if (action === "resume") return "Resume scheduled runs for";
  return "Run once for";
}

function SummaryStat({
  detail,
  label,
  value,
  tone,
}: {
  detail?: string;
  label: string;
  value: string | number;
  tone: "ok" | "warn" | "error" | "unknown" | "idle";
}) {
  // AlfredMetric carries its own card chrome; wrapping it in another Card
  // doubled the padding and ellipsized the labels at rail width.
  return <AlfredMetric detail={detail} label={label} tone={tone as AlfredTone} value={value} />;
}

function agentStats(rows: FleetControlRow[]) {
  return rows.reduce(
    (stats, row) => {
      if (row.service === "running") stats.running += 1;
      if (row.service === "paused") stats.paused += 1;
      if (row.service === "stopped") stats.stopped += 1;
      if (row.service === "unknown") stats.unknown += 1;
      if (isErrorStatus(row.summary?.status) || row.consecutiveFailures >= 2) {
        stats.erroring += 1;
      }
      stats.runsToday += row.summary?.firings_today ?? 0;
      return stats;
    },
    { running: 0, paused: 0, stopped: 0, unknown: 0, erroring: 0, runsToday: 0 },
  );
}

function scheduleCopy(row: FleetControlRow, schedule?: ScheduledRun): string {
  if (schedule?.cadence) return schedule.cadence;
  if (row.service === "running") return "Active";
  if (row.service === "paused") return "Paused";
  if (row.service === "stopped") return "Stopped";
  return "Unknown";
}

function scheduleMap(schedule: ScheduledRun[]): Record<string, ScheduledRun> {
  const map: Record<string, ScheduledRun> = {};
  for (const run of schedule) {
    const key = run.codename.trim();
    if (!key) continue;
    map[key] = run;
    map[key.split(".").pop() || key] = run;
  }
  return map;
}

function scheduleFor(
  map: Record<string, ScheduledRun>,
  codename: string,
): ScheduledRun | undefined {
  return map[codename] || map[codename.split(".").pop() || codename];
}

function editableScheduleValue(schedule?: ScheduledRun): string {
  const raw = (schedule?.raw_schedule || "").trim();
  if (!raw) return "30m";
  const interval = raw.match(/^interval:(\d+)$/);
  if (interval) {
    const seconds = Number(interval[1]);
    if (seconds > 0 && seconds % 86400 === 0) return `${seconds / 86400}d`;
    if (seconds > 0 && seconds % 3600 === 0) return `${seconds / 3600}h`;
    if (seconds > 0 && seconds % 60 === 0) return `${seconds / 60}m`;
  }
  const daily = raw.match(/^cron:(\d{1,2}):(\d{2})$/);
  if (daily) return `daily@${daily[1].padStart(2, "0")}:${daily[2]}`;
  const weekly = raw.match(/^cron:([0-6]):(\d{1,2}):(\d{2})$/);
  if (weekly) {
    const weekday = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"][Number(weekly[1])];
    return `weekly@${weekday}:${weekly[2].padStart(2, "0")}:${weekly[3]}`;
  }
  return raw;
}

function scheduleOptions(current: string): Array<{ value: string; label: string }> {
  if (!current || SCHEDULE_OPTIONS.some((option) => option.value === current)) {
    return SCHEDULE_OPTIONS;
  }
  return [{ value: current, label: `Current: ${current}` }, ...SCHEDULE_OPTIONS];
}

function agentActionCue(row: FleetControlRow): string {
  if (row.summary?.status === "error" || row.consecutiveFailures >= 2) {
    return "Open logs before the next run";
  }
  if (row.paused) {
    return "Resume or inspect logs";
  }
  if (row.service === "stopped") {
    return "Run once or check logs";
  }
  if (!row.summary) {
    return "Dry-run before first run";
  }
  return "Ready";
}
