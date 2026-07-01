import {
  AlertCircle,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  UserRound,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import {
  deleteCustomAgent,
  loadCustomAgents,
  saveCustomAgent,
  supportsNativeActions,
} from "../api";
import type {
  CustomAgentEngine,
  CustomAgentRecord,
  CustomAgentsResponse,
  CustomAgentWrite,
} from "../types";
import { Badge } from "./ui/badge";
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
import { Switch } from "./ui/switch";
import { Textarea } from "./ui/textarea";

const ENGINE_OPTIONS: Array<{ value: CustomAgentEngine; label: string; detail: string }> = [
  { value: "hybrid", label: "Hybrid", detail: "Route per task" },
  { value: "codex", label: "Codex", detail: "Code-heavy agent" },
  { value: "claude", label: "Claude", detail: "Planning and prose" },
];

const EMPTY_FORM = {
  codename: "",
  displayName: "",
  roleTitle: "",
  purpose: "",
  prompt: "",
  engine: "hybrid" as CustomAgentEngine,
  schedule: "30m",
  repos: "",
  enabled: true,
};

type CustomAgentForm = typeof EMPTY_FORM;

export function CustomAgentsPanel({
  baseUrl,
  onChanged,
}: {
  baseUrl: string;
  onChanged?: () => void;
}) {
  const canMutate = supportsNativeActions();
  const [snapshot, setSnapshot] = useState<CustomAgentsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editingCodename, setEditingCodename] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<CustomAgentRecord | null>(null);
  const [form, setForm] = useState<CustomAgentForm>(EMPTY_FORM);
  const deleteRef = useRef<HTMLButtonElement | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await loadCustomAgents(baseUrl, { includePrompt: true });
      setSnapshot(next);
    } catch (err) {
      if (!canMutate) {
        try {
          const fallback = await loadCustomAgents(baseUrl);
          setSnapshot(fallback);
          return;
        } catch {
          // Surface the original privileged-read failure; it usually carries
          // the clearest guidance about token/proxy state.
        }
      }
      setError(messageFromError(err));
    } finally {
      setLoading(false);
    }
  }, [baseUrl, canMutate]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const agents = snapshot?.agents || [];
  const isEditing = Boolean(editingCodename);
  const deployCue = useMemo(() => {
    if (!notice) return null;
    return notice.replace(/`/g, "");
  }, [notice]);

  const selectAgent = (agent: CustomAgentRecord) => {
    setEditingCodename(agent.codename);
    setForm(formFromAgent(agent));
    setNotice(null);
    setError(null);
  };

  const resetForm = () => {
    setEditingCodename(null);
    setForm(EMPTY_FORM);
    setNotice(null);
    setError(null);
  };

  const updateForm = <K extends keyof CustomAgentForm>(key: K, value: CustomAgentForm[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const payload = useMemo<CustomAgentWrite | null>(() => {
    const codename = form.codename.trim().toLowerCase().replace(/_/g, "-");
    const displayName = form.displayName.trim();
    const roleTitle = form.roleTitle.trim();
    const purpose = form.purpose.trim();
    const prompt = form.prompt.trim();
    const schedule = form.schedule.trim();
    if (!codename || !displayName || !roleTitle || !purpose || !prompt || !schedule) {
      return null;
    }
    return {
      codename,
      display_name: displayName,
      role_title: roleTitle,
      purpose,
      prompt,
      engine: form.engine,
      schedule,
      repos: parseRepos(form.repos),
      enabled: form.enabled,
    };
  }, [form]);

  const saveDisabled = !canMutate || !payload || saving;

  const onSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!payload || saveDisabled) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const response = await saveCustomAgent(baseUrl, payload);
      setEditingCodename(response.agent.codename);
      setForm(formFromAgent(response.agent));
      setNotice(response.detail);
      await refresh();
      onChanged?.();
    } catch (err) {
      setError(messageFromError(err));
    } finally {
      setSaving(false);
    }
  };

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    setError(null);
    setNotice(null);
    try {
      const response = await deleteCustomAgent(baseUrl, pendingDelete.codename);
      setNotice(response.detail);
      if (editingCodename === pendingDelete.codename) {
        setEditingCodename(null);
        setForm(EMPTY_FORM);
      }
      setPendingDelete(null);
      await refresh();
      onChanged?.();
    } catch (err) {
      setPendingDelete(null);
      setError(messageFromError(err));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <section className="custom-agents-panel" aria-label="Custom runtime agents">
      <header className="custom-agents-panel__header">
        <div className="custom-agents-panel__titleblock">
          <span className="custom-agents-panel__icon" aria-hidden="true">
            <UserRound />
          </span>
          <div>
            <p className="custom-agents-panel__eyebrow">Custom agents</p>
            <h2>Extend the fleet</h2>
            <p>
              Add local roles that run on the same scheduler, logs, memory, and
              provider guardrails as the built-in engineering team.
            </p>
          </div>
        </div>
        <div className="custom-agents-panel__actions">
          <Button type="button" variant="outline" size="sm" onClick={() => void refresh()}>
            <RefreshCw aria-hidden="true" />
            Refresh
          </Button>
          <Button type="button" size="sm" onClick={resetForm}>
            <Plus aria-hidden="true" />
            New agent
          </Button>
        </div>
      </header>

      {!canMutate ? (
        <div className="custom-agents-panel__callout" role="note">
          <AlertCircle aria-hidden="true" />
          Open the packaged desktop app to save or remove custom agents. Browser
          preview can read the inventory only.
        </div>
      ) : null}

      {error ? (
        <div className="custom-agents-panel__callout" data-tone="error" role="alert">
          <AlertCircle aria-hidden="true" />
          {error}
        </div>
      ) : null}

      {deployCue ? (
        <div className="custom-agents-panel__callout" data-tone="ok" role="status">
          <Save aria-hidden="true" />
          {deployCue}
        </div>
      ) : null}

      <div className="custom-agents-panel__body">
        <div className="custom-agents-panel__list" aria-busy={loading ? "true" : "false"}>
          <div className="custom-agents-panel__listhead">
            <span>{snapshot ? `${snapshot.count} configured` : "Inventory"}</span>
            {snapshot ? (
              <span>
                {snapshot.enabled_count} enabled, {snapshot.disabled_count} paused
              </span>
            ) : null}
          </div>
          {agents.length ? (
            <ul className="custom-agents-panel__rows" role="list">
              {agents.map((agent) => (
                <li key={agent.codename}>
                  <button
                    type="button"
                    className="custom-agents-panel__row"
                    data-selected={agent.codename === editingCodename ? "true" : "false"}
                    onClick={() => selectAgent(agent)}
                    aria-label={`Edit ${agent.display_name}`}
                  >
                    <span className="custom-agents-panel__row-main">
                      <span className="custom-agents-panel__row-title">
                        {agent.display_name}
                        <Badge variant="outline">{engineLabel(agent.engine)}</Badge>
                      </span>
                      <span>{agent.role_title}</span>
                    </span>
                    <span className="custom-agents-panel__row-meta">
                      <Badge variant="outline" data-tone={agent.enabled ? "ok" : "idle"}>
                        {agent.enabled ? "Enabled" : "Paused"}
                      </Badge>
                      <span>{scheduleLabel(agent.schedule)}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <div className="custom-agents-panel__empty">
              {loading ? "Loading custom agents..." : "No custom agents yet."}
            </div>
          )}
        </div>

        <form className="custom-agent-form" onSubmit={onSubmit}>
          <div className="custom-agent-form__head">
            <div>
              <p className="custom-agents-panel__eyebrow">
                {isEditing ? "Edit agent" : "New agent"}
              </p>
              <h3>{isEditing ? form.displayName || editingCodename : "Define a role"}</h3>
            </div>
            {isEditing ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={!canMutate}
                onClick={() => {
                  const agent = agents.find((item) => item.codename === editingCodename);
                  if (agent) setPendingDelete(agent);
                }}
              >
                <Trash2 aria-hidden="true" />
                Remove
              </Button>
            ) : null}
          </div>

          <div className="custom-agent-form__grid">
            <Field label="Codename" htmlFor="custom-agent-codename">
              <Input
                id="custom-agent-codename"
                value={form.codename}
                disabled={isEditing}
                required
                pattern="[a-z][a-z0-9-]{1,39}"
                placeholder="release-captain"
                onChange={(event) => updateForm("codename", event.target.value)}
              />
            </Field>
            <Field label="Display name" htmlFor="custom-agent-display-name">
              <Input
                id="custom-agent-display-name"
                value={form.displayName}
                required
                placeholder="Release Captain"
                onChange={(event) => updateForm("displayName", event.target.value)}
              />
            </Field>
            <Field label="Role title" htmlFor="custom-agent-role-title">
              <Input
                id="custom-agent-role-title"
                value={form.roleTitle}
                required
                placeholder="Release coordinator"
                onChange={(event) => updateForm("roleTitle", event.target.value)}
              />
            </Field>
            <Field label="Schedule" htmlFor="custom-agent-schedule">
              <Input
                id="custom-agent-schedule"
                value={form.schedule}
                required
                placeholder="30m, daily@09:00, weekly@mon:09:00"
                onChange={(event) => updateForm("schedule", event.target.value)}
              />
            </Field>
          </div>

          <fieldset className="custom-agent-form__engines" role="radiogroup">
            <legend>Engine</legend>
            {ENGINE_OPTIONS.map((option) => (
              <button
                type="button"
                role="radio"
                key={option.value}
                data-active={form.engine === option.value ? "true" : "false"}
                aria-checked={form.engine === option.value}
                onClick={() => updateForm("engine", option.value)}
              >
                <span>{option.label}</span>
                <small>{option.detail}</small>
              </button>
            ))}
          </fieldset>

          <Field label="Purpose" htmlFor="custom-agent-purpose">
            <Textarea
              id="custom-agent-purpose"
              value={form.purpose}
              required
              rows={2}
              placeholder="Checks release readiness before handoff."
              onChange={(event) => updateForm("purpose", event.target.value)}
            />
          </Field>

          <Field label="Prompt" htmlFor="custom-agent-prompt">
            <Textarea
              id="custom-agent-prompt"
              value={form.prompt}
              required
              rows={5}
              placeholder="Review the target repos for release blockers, summarize risk, and file follow-up tasks when needed."
              onChange={(event) => updateForm("prompt", event.target.value)}
            />
          </Field>

          <Field label="Repo scope" htmlFor="custom-agent-repos">
            <Textarea
              id="custom-agent-repos"
              value={form.repos}
              rows={3}
              placeholder="acme/api&#10;acme/web"
              onChange={(event) => updateForm("repos", event.target.value)}
            />
          </Field>

          <div className="custom-agent-form__footer">
            <label className="custom-agent-form__switch">
              <Switch
                checked={form.enabled}
                onCheckedChange={(checked) => updateForm("enabled", checked)}
                aria-label="Enable custom agent"
              />
              <span>{form.enabled ? "Enabled in scheduler" : "Saved but paused"}</span>
            </label>
            <Button type="submit" disabled={saveDisabled}>
              {saving ? (
                <RefreshCw aria-hidden="true" />
              ) : isEditing ? (
                <Save aria-hidden="true" />
              ) : (
                <Pencil aria-hidden="true" />
              )}
              {saving ? "Saving..." : isEditing ? "Save changes" : "Create agent"}
            </Button>
          </div>
        </form>
      </div>

      <Dialog
        open={Boolean(pendingDelete)}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
      >
        <DialogContent
          role="alertdialog"
          showCloseButton={false}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            deleteRef.current?.focus();
          }}
        >
          <DialogHeader>
            <DialogTitle>Remove {pendingDelete?.display_name || "custom agent"}?</DialogTitle>
            <DialogDescription>
              This deletes the custom-agent manifest entry. Run deploy afterwards
              to remove its scheduler job from this Mac.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setPendingDelete(null)}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={deleting}
              ref={deleteRef}
              onClick={() => void confirmDelete()}
            >
              {deleting ? "Removing..." : "Remove agent"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

function Field({
  children,
  htmlFor,
  label,
}: {
  children: ReactNode;
  htmlFor: string;
  label: string;
}) {
  return (
    <div className="custom-agent-form__field">
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
    </div>
  );
}

function formFromAgent(agent: CustomAgentRecord): CustomAgentForm {
  return {
    codename: agent.codename,
    displayName: agent.display_name,
    roleTitle: agent.role_title,
    purpose: agent.purpose,
    prompt: agent.prompt || "",
    engine: normalizeEngine(agent.engine),
    schedule: editableSchedule(agent.schedule),
    repos: agent.repos.join("\n"),
    enabled: agent.enabled,
  };
}

function normalizeEngine(value: string): CustomAgentEngine {
  return value === "claude" || value === "codex" || value === "hybrid" ? value : "hybrid";
}

function parseRepos(value: string): string[] {
  const seen = new Set<string>();
  for (const raw of value.split(/[\n,]+/)) {
    const repo = raw.trim();
    if (repo) seen.add(repo);
  }
  return Array.from(seen);
}

function editableSchedule(schedule: string): string {
  const interval = schedule.match(/^interval:(\d+)$/);
  if (interval) {
    const seconds = Number(interval[1]);
    if (seconds > 0 && seconds % 86400 === 0) return `${seconds / 86400}d`;
    if (seconds > 0 && seconds % 3600 === 0) return `${seconds / 3600}h`;
    if (seconds > 0 && seconds % 60 === 0) return `${seconds / 60}m`;
  }
  const daily = schedule.match(/^cron:(\d{1,2}):(\d{2})$/);
  if (daily) return `daily@${padHour(daily[1])}:${daily[2]}`;
  const weekly = schedule.match(/^cron:([0-6]):(\d{1,2}):(\d{2})$/);
  if (weekly) return `weekly@${weekdaySlug(weekly[1])}:${padHour(weekly[2])}:${weekly[3]}`;
  return schedule;
}

function scheduleLabel(schedule: string): string {
  const value = editableSchedule(schedule);
  if (/^\d+m$/.test(value)) return `Every ${value.replace("m", " min")}`;
  if (/^\d+h$/.test(value)) return `Every ${value.replace("h", " hr")}`;
  if (/^\d+d$/.test(value)) {
    const days = Number(value.slice(0, -1));
    return `Every ${days} ${days === 1 ? "day" : "days"}`;
  }
  if (value.startsWith("daily@")) return value.replace("daily@", "Daily ");
  if (value.startsWith("weekly@")) {
    const rest = value.slice("weekly@".length);
    const separator = rest.indexOf(":");
    if (separator === -1) return value.replace("weekly@", "Weekly ");
    const weekday = rest.slice(0, separator);
    const time = rest.slice(separator + 1);
    return `Weekly ${capitalize(weekday)} ${time}`.trim();
  }
  return value;
}

function padHour(value: string): string {
  return value.padStart(2, "0");
}

function weekdaySlug(value: string): string {
  return (
    {
      "0": "sun",
      "1": "mon",
      "2": "tue",
      "3": "wed",
      "4": "thu",
      "5": "fri",
      "6": "sat",
    }[value] || value
  );
}

function capitalize(value: string): string {
  return value ? `${value[0].toUpperCase()}${value.slice(1)}` : value;
}

function engineLabel(engine: string): string {
  const normalized = normalizeEngine(engine);
  return ENGINE_OPTIONS.find((option) => option.value === normalized)?.label || "Hybrid";
}

function messageFromError(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
