import { friendlyTime, plural, titleCase } from "../format";
import type { PlanDraft, ReliabilitySignal, Snapshot } from "../types";
import { localUrl } from "./links";
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

export function buildAttention(snapshot: Snapshot | null, baseUrl: string): AttentionItem[] {
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
    items.push(signalToAttention(signal, `action-${index}`));
  }
  for (const [index, signal] of (snapshot.actions.stale_workers || []).entries()) {
    items.push(signalToAttention(signal, `stale-${index}`, "run"));
  }
  items.push(...failurePatternsToAttention(snapshot.actions.failure_patterns || []));
  for (const plan of snapshot.plans.filter((plan) => planNeedsAttention(plan)).slice(0, 4)) {
    items.push({
      id: `plan-${plan.plan_id}`,
      label: titleCase(plan.status || "plan"),
      title: plan.title,
      detail: plan.preview || plan.affected_repos || "Review plan context before Alfred implements it.",
      tone: plan.status.includes("question") ? "warn" : "info",
      href: localUrl(baseUrl, `/plans/${plan.plan_id}`),
      icon: "plan",
    });
  }
  for (const [index, candidate] of (snapshot.memoryCandidates.rows || []).entries()) {
    items.push({
      id: `memory-${candidate.id}`,
      label: titleCase(candidate.severity || "Memory"),
      title: candidate.repo || "Review memory candidate",
      detail: candidate.body || `${candidate.codename}/${candidate.repo} from ${candidate.source}`,
      tone: candidate.severity === "blocker" ? "error" : "info",
      icon: "memory",
    });
    if (index >= 3) break;
  }
  for (const [index, signal] of (snapshot.actions.promotion_suggestions || []).entries()) {
    items.push(signalToAttention(signal, `memory-${index}`, "memory"));
  }

  return items.slice(0, 8);
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
