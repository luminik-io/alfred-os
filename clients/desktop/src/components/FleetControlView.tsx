import { Rows3, Workflow as WorkflowIcon } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { supportsNativeActions } from "../api";
import { friendlyTime } from "../format";
import { isErrorStatus } from "../lib/derive";
import {
  buildFleetRows,
  deriveFleetHealth,
  type FleetControlRow,
  type FleetServiceState,
} from "../lib/fleetControl";
import { agentProfile, type AgentProfile } from "../lib/agentProfile";
import {
  type CustomRosterNames,
  DEFAULT_ROSTER_THEME,
  EMPTY_CUSTOM_NAMES,
  type RosterThemeId,
} from "../lib/agentThemes";
import type { NativeActionRequest } from "../lib/uiTypes";
import { type WorkflowNodeInput } from "../lib/workflowGraph";
import type { AgentSummary, NativeAction, ScheduledRun } from "../types";
import { AgentDetailDrawer } from "./AgentDetailDrawer";
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
  rosterTheme = DEFAULT_ROSTER_THEME,
  customNames = EMPTY_CUSTOM_NAMES,
  onRunLocalAction,
  onViewLogs,
}: {
  agents: AgentSummary[];
  schedule?: ScheduledRun[];
  service: FleetServiceState;
  nativeBusy: string | null;
  // The active roster theme (name + role-label display layer). Defaults to the shipped
  // Batman roster so an omitted prop renders exactly as before.
  rosterTheme?: RosterThemeId;
  // Operator-authored names/roles for the `custom` theme; ignored by presets.
  customNames?: CustomRosterNames;
  onRunLocalAction: (request: NativeActionRequest) => void;
  onViewLogs: (codename: string) => void;
}) {
  const canRun = supportsNativeActions();
  const rows = buildFleetRows(agents, service);
  const scheduleByCodename = useMemo(() => scheduleMap(schedule || []), [schedule]);
  // Theme-bound profile resolver so the list rows and the drawer render the same
  // themed name + plain role label as the canvas, with the two-arg call shape
  // those call sites already use.
  const themedProfile = useCallback(
    (row: FleetControlRow, sched?: ScheduledRun) =>
      agentProfile(row, sched, rosterTheme, customNames),
    [rosterTheme, customNames],
  );
  const health = deriveFleetHealth(rows);
  const stats = agentStats(rows);
  const defaultSelected = defaultSelectedCodename(rows);
  const [selectedCodename, setSelectedCodename] = useState<string | null>(defaultSelected);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [pending, setPending] = useState<PendingAction>(null);
  const [viewMode, setViewMode] = useState<RosterView>(() => readRosterView());
  const affirmRef = useRef<HTMLButtonElement | null>(null);

  // Selecting an agent (canvas node or list row) opens the detail drawer.
  const selectAgent = (codename: string) => {
    setSelectedCodename(codename);
    setDrawerOpen(true);
  };

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

  // Live display data for EVERY agent the fleet reports (not a hardcoded
  // subset), derived with the same helpers the cards use so status/accent/runs
  // stay consistent across views. Each agent's lane/role and themed name come
  // from its own metadata via the active roster theme.
  const workflowInputs = useMemo<WorkflowNodeInput[]>(
    () =>
      rows.map((row) => {
        const profile = themedProfile(row, scheduleFor(scheduleByCodename, row.codename));
        const { tone, label } = serviceTone(row);
        return {
          codename: row.codename,
          role: profile.role,
          label: profile.name,
          roleLabel: profile.roleLabel,
          accent: profile.themeAccent,
          tone,
          statusLabel: label,
          runsToday: row.summary?.firings_today ?? 0,
          lastRunLabel: row.summary ? friendlyTime(row.summary.last_run_at) : "No run",
          failStreak: row.consecutiveFailures,
        };
      }),
    [rows, scheduleByCodename, themedProfile],
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
                selectedCodename={drawerOpen ? selectedRow?.codename ?? null : null}
                onSelect={selectAgent}
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
                      profile={themedProfile}
                      onSelect={() => selectAgent(row.codename)}
                    />
                  ))}
                </div>
              </div>
            )}
          </div>

          <AgentDetailDrawer
            row={selectedRow}
            open={drawerOpen}
            onOpenChange={setDrawerOpen}
            schedule={selectedSchedule}
            canRun={canRun}
            nativeBusy={nativeBusy}
            serviceTone={serviceTone}
            agentProfile={themedProfile}
            agentActionCue={agentActionCue}
            scheduleCopy={scheduleCopy}
            editableScheduleValue={editableScheduleValue}
            scheduleOptions={scheduleOptions}
            onDispatch={dispatch}
            onViewLogs={onViewLogs}
          />
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
  profile: resolveProfile,
  row,
  schedule,
  selected,
}: {
  onSelect: () => void;
  profile: (row: FleetControlRow, schedule?: ScheduledRun) => AgentProfile;
  row: FleetControlRow;
  schedule?: ScheduledRun;
  selected: boolean;
}) {
  const profile = resolveProfile(row, schedule);
  const { tone, label } = serviceTone(row);
  return (
    <button
      type="button"
      className="agents-deck__row"
      data-selected={selected ? "true" : "false"}
      style={{ "--agent-accent": profile.themeAccent } as React.CSSProperties}
      onClick={onSelect}
      aria-current={selected ? "true" : undefined}
      aria-label={`Select ${profile.name}, ${profile.roleLabel}`}
    >
      <span className="agents-deck__row-mark" aria-hidden="true" />
      <span className="min-w-0">
        <span className="agents-deck__row-title">
          {profile.name}
          {profile.roleLabel ? (
            <span className="agents-deck__row-role">{profile.roleLabel}</span>
          ) : null}
        </span>
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
