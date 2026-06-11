import { Clock3, Pause, Play, RotateCw, ScrollText, Square } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { supportsNativeActions } from "../api";
import { exactTime, friendlyTime, titleCase } from "../format";
import {
  buildFleetRows,
  deriveFleetHealth,
  type FleetControlRow,
  type FleetServiceState,
} from "../lib/fleetControl";
import type { NativeActionRequest } from "../lib/uiTypes";
import type { AgentSummary, NativeAction, ScheduledRun } from "../types";
import { AlfredMetric, AlfredStatusDot, type AlfredTone } from "./ui/alfred";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "./ui/card";
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
  const [pending, setPending] = useState<PendingAction>(null);
  const affirmRef = useRef<HTMLButtonElement | null>(null);

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
    <section className="space-y-4" aria-label="Agent roster">
      <div className="grid gap-3 md:grid-cols-5">
        <SummaryStat label="Health" value={health.summary} tone={health.level} />
        <SummaryStat label="Running" value={stats.running} tone={stats.running ? "ok" : "idle"} />
        <SummaryStat
          label="Paused"
          value={stats.paused + stats.stopped}
          tone={stats.paused || stats.stopped ? "warn" : "ok"}
        />
        <SummaryStat label="Needs logs" value={stats.erroring} tone={stats.erroring ? "error" : "ok"} />
        <SummaryStat label="Runs today" value={stats.runsToday} tone="idle" />
      </div>

      {rows.length ? (
        <div className="grid gap-3 xl:grid-cols-2" role="list" aria-label="Agents">
          {rows.map((row) => (
            <FleetCard
              key={row.codename}
              row={row}
              schedule={scheduleFor(scheduleByCodename, row.codename)}
              canRun={canRun}
              nativeBusy={nativeBusy}
              onDispatch={dispatch}
              onViewLogs={onViewLogs}
            />
          ))}
        </div>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>No agents detected</CardTitle>
            <CardDescription>Connect to Alfred serve to load the roster.</CardDescription>
          </CardHeader>
        </Card>
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

function FleetCard({
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
  return (
    <Card className="border-border/70 bg-card/80 shadow-sm backdrop-blur" role="listitem">
      <CardHeader className="gap-3">
        <div className="flex min-w-0 items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="truncate">{row.codename}</CardTitle>
            <CardDescription className="truncate">{agentActionCue(row)}</CardDescription>
          </div>
          <ServiceBadge row={row} />
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid grid-cols-2 gap-3 text-sm lg:grid-cols-4">
          <MetaItem label="Last run" title={exactTime(row.summary?.last_run_at)}>
            {row.summary ? friendlyTime(row.summary.last_run_at) : "No run"}
          </MetaItem>
          <MetaItem label="Runs today">{row.summary?.firings_today ?? 0}</MetaItem>
          <MetaItem label="Fail streak">{row.consecutiveFailures}</MetaItem>
          <MetaItem label="Schedule">{scheduleCopy(row, schedule)}</MetaItem>
        </dl>

        <p className="line-clamp-2 min-h-11 text-sm text-muted-foreground">
          {row.paused && row.pausedSince
            ? `Paused since ${friendlyTime(row.pausedSince)}.`
            : row.summary?.last_summary || "No runs yet."}
        </p>

        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" variant="ghost" onClick={() => onViewLogs(row.codename)}>
            <ScrollText aria-hidden="true" />
            View logs
          </Button>
          {canRun ? (
            <>
              {row.paused ? (
                <Button
                  type="button"
                  variant="outline"
                  disabled={busy("resume")}
                  onClick={() => onDispatch("resume", row.codename, "Resume")}
                >
                  <Play aria-hidden="true" />
                  {busy("resume") ? "Resuming" : "Resume"}
                </Button>
              ) : (
                <Button
                  type="button"
                  variant="outline"
                  disabled={busy("pause")}
                  onClick={() => onDispatch("pause", row.codename, "Pause")}
                >
                  <Pause aria-hidden="true" />
                  {busy("pause") ? "Pausing" : "Pause"}
                </Button>
              )}
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
          <div className="grid gap-2 rounded-lg border border-border/70 bg-muted/35 p-3 sm:grid-cols-[1fr_auto] sm:items-end">
            <div className="space-y-1.5">
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
      </CardContent>
    </Card>
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
  if (row.summary?.status === "error" || row.consecutiveFailures >= 2) {
    return { tone: "error", label: "Error" };
  }
  if (row.service === "paused") {
    return { tone: "warn", label: "Paused" };
  }
  if (row.service === "stopped") {
    return { tone: "warn", label: "Stopped" };
  }
  if (row.service === "running") {
    return { tone: "ok", label: "Running" };
  }
  return { tone: "idle", label: titleCase(row.summary?.status || "idle") };
}

function confirmCopy(action: NativeAction): string {
  if (action === "pause") return "Pause scheduled runs for";
  if (action === "resume") return "Resume scheduled runs for";
  return "Run once for";
}

function SummaryStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | number;
  tone: "ok" | "warn" | "error" | "unknown" | "idle";
}) {
  return (
    <Card size="sm" className="border-border/70 bg-card/75 shadow-sm backdrop-blur">
      <CardContent>
        <AlfredMetric
          className="border-0 bg-transparent p-0 shadow-none"
          label={label}
          tone={tone as AlfredTone}
          value={value}
        />
      </CardContent>
    </Card>
  );
}

function agentStats(rows: FleetControlRow[]) {
  return rows.reduce(
    (stats, row) => {
      if (row.service === "running") stats.running += 1;
      if (row.service === "paused") stats.paused += 1;
      if (row.service === "stopped") stats.stopped += 1;
      if (row.service === "unknown") stats.unknown += 1;
      if (row.summary?.status === "error" || row.consecutiveFailures >= 2) {
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
