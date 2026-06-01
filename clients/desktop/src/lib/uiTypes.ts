import type { NativeAction } from "../types";

export type TabKey = "home" | "compose" | "plans" | "memory" | "fleet" | "logs" | "setup";

export type StatCard = { label: string; value: string; detail: string };

export type FollowupAction = "convert" | "handled";

export type ActionNotice = { tone: "ok" | "error"; message: string } | null;

export type NativeActionRequest = {
  action: NativeAction;
  target?: string;
  refreshAfter?: boolean;
};

export type AttentionItem = {
  id: string;
  label: string;
  title: string;
  detail: string;
  tone: "ok" | "warn" | "error" | "info";
  command?: string;
  href?: string;
  targetTab?: TabKey;
  icon: "plan" | "run" | "memory" | "setup";
};
