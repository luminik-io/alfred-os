import type { NativeAction } from "../types";

export type TabKey =
  | "now"
  | "compose"
  | "plans"
  | "runs"
  | "agents"
  | "fleet"
  | "activity"
  | "memory"
  | "setup";

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
  icon: "plan" | "run" | "memory" | "setup";
};
