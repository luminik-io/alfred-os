import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  FALLBACK_BASE_URL,
  addTrustedSlackUser,
  convertFollowupToDraft,
  errorDetail,
  initialBaseUrl,
  isDefaultBaseUrl,
  loadSnapshot,
  markFollowupHandled,
  promoteMemoryCandidate,
  rememberBaseUrl,
  removeTrustedSlackUser,
  rejectMemoryCandidate,
  runNativeAction,
  setTrayStatus,
  startLocalRuntime,
  supportsNativeActions,
} from "../api";
import { buildAttention, buildStats } from "../lib/derive";
import {
  buildFleetRows,
  deriveFleetHealth,
  parseFleetServiceState,
  type FleetServiceState,
} from "../lib/fleetControl";
import {
  buildFeed,
  countUnseen,
  loadSeenIds,
  markAllSeen,
  persistSeenIds,
} from "../lib/notifications";
import { listenTrayEvents } from "../lib/trayEvents";
import type { ActionNotice, FollowupAction, NativeActionRequest } from "../lib/uiTypes";
import type { NativeCommandResult, PlanDraft, Snapshot } from "../types";

const POLL_INTERVAL_MS = 60_000;

export type UseAlfred = ReturnType<typeof useAlfred>;

export function useAlfred() {
  const [baseUrl, setBaseUrl] = useState(initialBaseUrl);
  const [serverInput, setServerInput] = useState(initialBaseUrl);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errorRaw, setErrorRaw] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyPlanAction, setBusyPlanAction] = useState<string | null>(null);
  const [busyMemoryAction, setBusyMemoryAction] = useState<string | null>(null);
  const [busyTrustedUser, setBusyTrustedUser] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<ActionNotice>(null);
  const [nativeBusy, setNativeBusy] = useState<string | null>(null);
  const [nativeResult, setNativeResult] = useState<NativeCommandResult | null>(null);
  const [nativeError, setNativeError] = useState<string | null>(null);
  const [nativeErrorRaw, setNativeErrorRaw] = useState<string | null>(null);
  const [fleetService, setFleetService] = useState<FleetServiceState>({});
  const [seenIds, setSeenIds] = useState<Set<string>>(() => loadSeenIds());

  // Monotonic request id so a slow background poll cannot clobber newer state.
  // A 60s poll started before the user paused an agent could otherwise resolve
  // *after* the post-pause refresh and re-show the agent as running. Each call
  // claims an id; only the latest id is allowed to commit its result.
  const reqRef = useRef(0);

  const refresh = useCallback(
    async (nextBaseUrl = baseUrl) => {
      const targetBaseUrl = nextBaseUrl.trim();
      const id = ++reqRef.current;
      setLoading(true);
      setError(null);
      setErrorRaw(null);
      try {
        try {
          const next = await loadSnapshot(targetBaseUrl);
          if (id !== reqRef.current) return;
          setSnapshot(next);
          setBaseUrl(targetBaseUrl);
          setServerInput(targetBaseUrl);
          rememberBaseUrl(targetBaseUrl);
        } catch (firstErr) {
          if (isDefaultBaseUrl(targetBaseUrl)) {
            const next = await loadSnapshot(FALLBACK_BASE_URL);
            if (id !== reqRef.current) return;
            setSnapshot(next);
            setBaseUrl(FALLBACK_BASE_URL);
            setServerInput(FALLBACK_BASE_URL);
            rememberBaseUrl(FALLBACK_BASE_URL);
          } else {
            throw firstErr;
          }
        }
      } catch (err) {
        if (id !== reqRef.current) return;
        setError(err instanceof Error ? err.message : String(err));
        setErrorRaw(errorDetail(err));
      } finally {
        // Only the latest request owns the loading flag; a superseded poll
        // resolving late must not flip the spinner off mid-refresh.
        if (id === reqRef.current) {
          setLoading(false);
        }
      }
    },
    [baseUrl],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const runFollowupAction = useCallback(
    async (plan: PlanDraft, action: FollowupAction) => {
      const key = `${plan.plan_id}:${action}`;
      setBusyPlanAction(key);
      setActionNotice(null);
      try {
        const result =
          action === "convert"
            ? await convertFollowupToDraft(baseUrl, plan.plan_id)
            : await markFollowupHandled(baseUrl, plan.plan_id);
        const message =
          action === "convert"
            ? `Created planning draft ${result.draft_id || "for the next pass"}.`
            : "Marked the follow-up handled and moved it out of the inbox.";
        setActionNotice({ tone: "ok", message });
        await refresh(baseUrl);
      } catch (err) {
        setActionNotice({
          tone: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      } finally {
        setBusyPlanAction(null);
      }
    },
    [baseUrl, refresh],
  );

  const addTrustedUser = useCallback(
    async (userId: string) => {
      const clean = userId.trim();
      if (!clean) return;
      setBusyTrustedUser(`add:${clean}`);
      setActionNotice(null);
      try {
        const result = await addTrustedSlackUser(baseUrl, clean);
        setActionNotice({
          tone: "ok",
          message: result.added
            ? `Trusted Slack collaborator ${clean}.`
            : `${clean} was already trusted locally.`,
        });
        await refresh(baseUrl);
      } catch (err) {
        setActionNotice({
          tone: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      } finally {
        setBusyTrustedUser(null);
      }
    },
    [baseUrl, refresh],
  );

  const runMemoryCandidateAction = useCallback(
    async (candidateId: string, action: "promote" | "reject") => {
      const key = `${candidateId}:${action}`;
      setBusyMemoryAction(key);
      setActionNotice(null);
      try {
        await (action === "promote"
          ? promoteMemoryCandidate(baseUrl, candidateId)
          : rejectMemoryCandidate(baseUrl, candidateId));
        setActionNotice({
          tone: "ok",
          message:
            action === "promote"
              ? `Promoted memory candidate ${candidateId}.`
              : `Rejected memory candidate ${candidateId}.`,
        });
        await refresh(baseUrl);
      } catch (err) {
        setActionNotice({
          tone: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      } finally {
        setBusyMemoryAction(null);
      }
    },
    [baseUrl, refresh],
  );

  const removeTrustedUser = useCallback(
    async (userId: string) => {
      setBusyTrustedUser(`remove:${userId}`);
      setActionNotice(null);
      try {
        const result = await removeTrustedSlackUser(baseUrl, userId);
        setActionNotice({
          tone: result.removed ? "ok" : "error",
          message: result.removed
            ? `Removed local trusted collaborator ${userId}.`
            : `${userId} is not a removable local collaborator.`,
        });
        await refresh(baseUrl);
      } catch (err) {
        setActionNotice({
          tone: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      } finally {
        setBusyTrustedUser(null);
      }
    },
    [baseUrl, refresh],
  );

  // Paused/running state now comes from the polled /api/status feed (the server
  // reads the same pause marker the CLI writes). This optional read only enriches
  // the CLI-only fail-streak counter the API does not expose, on desktop builds.
  // Best-effort: a failure leaves the previous service map in place.
  const refreshFleetService = useCallback(async () => {
    if (!supportsNativeActions()) {
      return;
    }
    try {
      const result = await runNativeAction("status");
      const parsed = parseFleetServiceState(result);
      if (Object.keys(parsed).length) {
        setFleetService(parsed);
      }
    } catch {
      // Leave the last known service state; the panel still renders summaries.
    }
  }, []);

  const runLocalAction = useCallback(
    async ({ action, target, refreshAfter = false }: NativeActionRequest) => {
      const key = `${action}:${target || "fleet"}`;
      setNativeBusy(key);
      setNativeError(null);
      setNativeErrorRaw(null);
      setNativeResult(null);
      try {
        const result = await runNativeAction(action, target);
        setNativeResult(result);
        if (refreshAfter) {
          await refresh(baseUrl);
        }
        // Paused/loaded badges update from the /api/status re-poll above. After a
        // service-changing verb, also refresh the CLI-only fail-streak counter
        // (resume resets it) so the health rollup matches the new reality.
        if (action === "pause" || action === "resume" || action === "run") {
          await refreshFleetService();
        }
      } catch (err) {
        setNativeError(err instanceof Error ? err.message : String(err));
        setNativeErrorRaw(errorDetail(err));
      } finally {
        setNativeBusy(null);
      }
    },
    [baseUrl, refresh, refreshFleetService],
  );

  const startRuntime = useCallback(async () => {
    setNativeBusy("runtime:start");
    setNativeError(null);
    setNativeErrorRaw(null);
    setNativeResult(null);
    try {
      const result = await startLocalRuntime();
      setNativeResult(result);
      window.setTimeout(() => void refresh("http://127.0.0.1:7010"), 900);
    } catch (err) {
      setNativeError(err instanceof Error ? err.message : String(err));
      setNativeErrorRaw(errorDetail(err));
    } finally {
      setNativeBusy(null);
    }
  }, [refresh]);

  const attention = useMemo(() => buildAttention(snapshot, baseUrl), [snapshot, baseUrl]);
  const stats = useMemo(() => buildStats(snapshot), [snapshot]);

  const feed = useMemo(() => buildFeed(snapshot), [snapshot]);
  const unseenCount = useMemo(() => countUnseen(feed, seenIds), [feed, seenIds]);

  const fleetRows = useMemo(
    () => buildFleetRows(snapshot?.status.agents || [], fleetService),
    [snapshot, fleetService],
  );
  const fleetHealth = useMemo(() => deriveFleetHealth(fleetRows), [fleetRows]);

  const markActivitySeen = useCallback(() => {
    setSeenIds((prev) => {
      const next = markAllSeen(feed, prev);
      persistSeenIds(next, feed);
      return next;
    });
  }, [feed]);

  // Read service state once on mount (desktop only); polling refreshes happen
  // through runLocalAction after any pause/resume/run.
  const fetchedServiceRef = useRef(false);
  useEffect(() => {
    if (fetchedServiceRef.current) {
      return;
    }
    fetchedServiceRef.current = true;
    void refreshFleetService();
  }, [refreshFleetService]);

  // Mirror fleet health onto the menu-bar tray whenever it changes.
  useEffect(() => {
    void setTrayStatus(fleetHealth.level, fleetHealth.summary);
  }, [fleetHealth.level, fleetHealth.summary]);

  // Tray quick actions (pause-all / resume-all) reuse the validated action path.
  useEffect(() => {
    const unlisten = listenTrayEvents({
      onPauseAll: () => runLocalAction({ action: "pause", target: "all", refreshAfter: true }),
      onResumeAll: () => runLocalAction({ action: "resume", target: "all", refreshAfter: true }),
    });
    return () => {
      void unlisten.then((off) => off());
    };
  }, [runLocalAction]);

  return {
    baseUrl,
    serverInput,
    setServerInput,
    snapshot,
    error,
    errorRaw,
    loading,
    busyPlanAction,
    busyMemoryAction,
    busyTrustedUser,
    actionNotice,
    nativeBusy,
    nativeResult,
    nativeError,
    nativeErrorRaw,
    attention,
    stats,
    fleetService,
    fleetRows,
    fleetHealth,
    feed,
    unseenCount,
    seenIds,
    markActivitySeen,
    refresh,
    refreshFleetService,
    runFollowupAction,
    runMemoryCandidateAction,
    addTrustedUser,
    removeTrustedUser,
    runLocalAction,
    startRuntime,
  };
}
