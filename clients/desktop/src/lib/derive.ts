import { friendlyTime, plural, titleCase } from "../format";
import type { PlanDraft, ReliabilitySignal, Snapshot } from "../types";
import type { AttentionItem } from "./uiTypes";

export function buildStats(snapshot: Snapshot | null) {
  const agents = snapshot?.status.agents || [];
  const reliability = snapshot?.actions;
  const live = agents.filter((agent) => agent.status === "live").length;
  const errored = agents.filter((agent) => agent.status === "error").length;
  return [
    {
      label: "Agents",
      value: agents.length ? `${live}/${agents.length}` : "0",
      detail: agents.length ? `${plural(errored, "error")} visible` : "waiting for state",
    },
    {
      label: "Runs today",
      value: String(snapshot?.status.total_today || 0),
      detail: snapshot ? `updated ${friendlyTime(snapshot.loadedAt.toISOString())}` : "not loaded",
    },
    {
      label: "Planning",
      value: String(snapshot?.plans.length || 0),
      detail: "saved plans and follow-ups",
    },
    {
      label: "Memory",
      value: String(snapshot?.memoryCandidates.rows.length || reliability?.promotion_suggestions?.length || 0),
      detail: "review candidates",
    },
  ];
}

export function buildAttention(snapshot: Snapshot | null): AttentionItem[] {
  if (!snapshot) {
    return [
      {
        id: "connect",
        label: "Setup",
        title: "Connect to the local Alfred server",
        detail: "Start alfred serve so the client can read local state.",
        tone: "warn",
        command: "alfred serve --no-browser",
        icon: "setup",
      },
    ];
  }

  const items: AttentionItem[] = [];
  for (const [index, signal] of (snapshot.actions.actions || []).entries()) {
    if (signal.kind === "failure_pattern") continue;
    if (signal.kind === "memory_promotion") continue;
    items.push(signalToAttention(signal, `action-${index}`));
  }
  for (const [index, signal] of (snapshot.actions.stale_workers || []).entries()) {
    items.push(signalToAttention(signal, `stale-${index}`, "run"));
  }
  items.push(...failurePatternsToAttention(snapshot.actions.failure_patterns || []));
  const waitingPlans = snapshot.plans.filter((plan) => planNeedsAttention(plan));
  if (waitingPlans.length === 1) {
    const plan = waitingPlans[0];
    items.push({
      id: `plan-${plan.plan_id}`,
      label: titleCase(plan.status || "plan"),
      title: plan.title,
      detail: plan.preview || plan.affected_repos || "Review plan context before Alfred implements it.",
      tone: plan.status.includes("question") ? "warn" : "info",
      targetTab: "plans",
      icon: "plan",
    });
  } else if (waitingPlans.length > 1) {
    items.push({
      id: "plans-review",
      label: "Planning queue",
      title: `${plural(waitingPlans.length, "plan")} waiting`,
      detail: waitingPlans
        .slice(0, 3)
        .map((plan) => plan.title)
        .join("; "),
      tone: waitingPlans.some((plan) => plan.status.toLowerCase().includes("question"))
        ? "warn"
        : "info",
      targetTab: "plans",
      icon: "plan",
    });
  }
  const candidates = snapshot.memoryCandidates.rows || [];
  if (candidates.length) {
    const repos = Array.from(new Set(candidates.map((candidate) => candidate.repo).filter(Boolean)));
    items.push({
      id: "memory-review",
      label: "Review queue",
      title: `${plural(candidates.length, "memory candidate")} ready`,
      detail: repos.length
        ? `Review before promotion: ${repos.slice(0, 3).join(", ")}${repos.length > 3 ? ", ..." : ""}.`
        : "Review candidates before they enter recall.",
      tone: candidates.some((candidate) => candidate.severity === "blocker") ? "error" : "info",
      targetTab: "memory",
      icon: "memory",
    });
  }
  const suggestions = snapshot.actions.promotion_suggestions || [];
  if (!candidates.length && suggestions.length) {
    items.push({
      id: "memory-suggestions",
      label: "Review queue",
      title: `${plural(suggestions.length, "memory suggestion")} ready`,
      detail: "Review fleet-brain suggestions before promotion.",
      tone: "info",
      targetTab: "memory",
      icon: "memory",
    });
  }

  return items.slice(0, 6);
}

export function failurePatternsToAttention(signals: ReliabilitySignal[]): AttentionItem[] {
  const grouped = new Map<string, ReliabilitySignal[]>();
  for (const signal of signals) {
    const agent = signal.agent || signal.codename || signal.target || "fleet";
    grouped.set(agent, [...(grouped.get(agent) || []), signal]);
  }

  return Array.from(grouped.entries()).map(([agent, group]) => {
    const total = group.reduce((sum, signal) => sum + (signal.count || 1), 0);
    const labels = group.map(patternLabel).filter(Boolean);
    const latest = latestTimestamp(group);
    const multiple = group.length > 1;
    const severity = group.some((signal) => signal.severity === "error" || signal.severity === "blocker")
      ? "error"
      : "warn";
    return {
      id: `failure-${agent}`,
      label: "Needs inspection",
      title: `${titleCase(agent)} reliability signal`,
      detail: multiple
        ? `${group.length} repeated patterns, ${total} events: ${labels.join(", ")}${
            latest ? `; last seen ${friendlyTime(latest)}` : ""
          }.`
        : `${labels[0] || "failure"} repeated ${total} time${total === 1 ? "" : "s"}${
            latest ? `; last seen ${friendlyTime(latest)}` : ""
          }.`,
      command: group.find((signal) => signal.command)?.command,
      tone: severity,
      icon: "run",
    };
  });
}

export function signalToAttention(
  signal: ReliabilitySignal,
  id: string,
  icon: AttentionItem["icon"] = "setup",
  tone: AttentionItem["tone"] = "warn",
): AttentionItem {
  return {
    id,
    label: titleCase(signal.severity || signal.codename || "Action"),
    title: titleCase(signal.title || signal.action || signal.codename || "Review Alfred signal"),
    detail: signal.message || signal.summary || signal.reason || "Open the local source before changing state.",
    command: signal.command,
    tone,
    icon,
  };
}

function patternLabel(signal: ReliabilitySignal): string {
  return (
    signal.subtype ||
    signal.latest_summary ||
    signal.summary ||
    signal.reason ||
    signal.action ||
    "failure"
  );
}

function latestTimestamp(signals: ReliabilitySignal[]): string | null {
  const timestamps = signals
    .map((signal) => signal.last_seen || signal.first_seen || null)
    .filter((value): value is string => Boolean(value));
  if (!timestamps.length) return null;
  return timestamps.sort((a, b) => new Date(b).getTime() - new Date(a).getTime())[0];
}

export function planNeedsAttention(plan: PlanDraft): boolean {
  const status = plan.status.toLowerCase();
  return (
    status.includes("draft") ||
    status.includes("follow") ||
    status.includes("question") ||
    status.includes("blocked")
  );
}
