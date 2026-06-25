import { createContext, useContext } from "react";

import type { FileNotice } from "./useAskThread";

// Surface state the inline draft card needs, threaded down through React context
// because assistant-ui renders tool-call parts (the draft card) deep inside the
// MessagePrimitive tree where props cannot reach. The view provides this once
// around the thread.
export type AskSurface = {
  fileBusy: boolean;
  fileNotice: FileNotice | null;
  // The tool-call id of the most recent draft card; the file notice rides this
  // card so a filed confirmation survives conversational follow-ups.
  lastDraftToolCallId: string | null;
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
