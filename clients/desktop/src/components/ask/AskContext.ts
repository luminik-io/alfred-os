import { createContext, useContext } from "react";

import type { FileNotice } from "./useAskThread";

// Surface state the inline draft card needs, threaded down through React context
// because assistant-ui renders tool-call parts (the draft card) deep inside the
// MessagePrimitive tree where props cannot reach. The view provides this once
// around the thread.
export type AskSurface = {
  // The draftId currently being filed (or null), so each card shows its own
  // spinner without disabling the others.
  fileBusyId: string | null;
  // File results keyed by draftId, so each card shows only its own notice
  // instead of a single global one that drifts onto the last card.
  fileNotices: Record<string, FileNotice>;
  onFile: (draftId: string) => void;
  onOpenWork: () => void;
};

const AskSurfaceContext = createContext<AskSurface | null>(null);

export const AskSurfaceProvider = AskSurfaceContext.Provider;

export function useAskSurface(): AskSurface {
  const value = useContext(AskSurfaceContext);
  if (!value) {
    throw new Error("useAskSurface must be used within an AskSurfaceProvider");
  }
  return value;
}
