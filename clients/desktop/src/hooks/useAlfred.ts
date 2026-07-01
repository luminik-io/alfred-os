import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  addTrustedSlackUser,
  clientBaseUrl,
  convertFollowupToDraft,
  decidePlan,
  discardPlan,
  errorDetail,
  installAlfredCore,
  filePlanIssue,
  initialBaseUrl,
  loadShipped,
  loadSnapshot,
  loadUsage,
  markFollowupHandled,
  promoteMemoryCandidate,
  rememberBaseUrl,
  removeTrustedSlackUser,
  rejectMemoryCandidate,
  runNativeAction,
  setQueuePickup,
  setTrayStatus,
  startLocalRuntime,
  supportsNativeActions,
} from "../api";
import { buildNeedsYou } from "../lib/derive";
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
import type {
  ActionNotice,
  FollowupAction,
  NativeActionRequest,
  NoticeDomain,
} from "../lib/uiTypes";
import type {
  AssignmentTargetAgent,
  NativeCommandResult,
  PlanDecision,
  PlanDraft,
  QueueAction,
  QueueActionResponse,
  ShippedBoard,
  Snapshot,
  UsageResponse,
} from "../types";

const POLL_INTERVAL_MS = 60_000;
type ShippedRefreshOptions = { demo?: boolean };

export type UseAlfred = ReturnType<typeof useAlfred>;

function assignmentLabel(target: AssignmentTargetAgent | string): string {
  if (target === "batman") return "Batman";
  if (target === "lucius") return "Lucius";
  return "Alfred";
}

function assignmentNoticeMessage(
  repo: string,
  issueNumber: number,
  requestedTarget: AssignmentTargetAgent,
  result: QueueActionResponse,
): string {
  const routedTarget =
    result.target_agent || (requestedTarget !== "auto" ? requestedTarget : "");
  if (routedTarget === "batman" || routedTarget === "lucius") {
    return `Assigned ${repo}#${issueNumber} to ${assignmentLabel(routedTarget)}.`;
  }
  const detail = result.detail?.trim();
  if (detail) return detail;
  return `${repo}#${issueNumber} needs human scoping before an agent can pick it up.`;
}

export function useAlfred() {
  const [baseUrl, setBaseUrl] = useState(initialBaseUrl);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errorRaw, setErrorRaw] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyPlanAction, setBusyPlanAction] = useState<string | null>(null);
  const [busyMemoryAction, setBusyMemoryAction] = useState<string | null>(null);
  // Synchronous mirror of "a memory action is in flight". State updates are
  // batched, so two clicks in the same tick could both read a stale null from
  // busyMemoryAction; a ref flips immediately and keeps the guard airtight.
  const busyMemoryRef = useRef(false);
  const [busyTrustedUser, setBusyTrustedUser] = useState<string | null>(null);
  const [busyQueue, setBusyQueue] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<ActionNotice>(null);
  const [nativeBusy, setNativeBusy] = useState<string | null>(null);
  const [nativeResult, setNativeResult] = useState<NativeCommandResult | null>(null);
  const [nativeError, setNativeError] = useState<string | null>(null);
  const [nativeErrorRaw, setNativeErrorRaw] = useState<string | null>(null);
  const [fleetService, setFleetService] = useState<FleetServiceState>({});
  const [seenIds, setSeenIds] = useState<Set<string>>(() => loadSeenIds());
  // The Kanban board is fetched independently of the core snapshot (its
  // multi-repo gh scan is slow), with its own state so it never blocks the view.
  const [shipped, setShipped] = useState<ShippedBoard | null>(null);
  const [shippedState, setShippedState] = useState<"idle" | "loading" | "error">("idle");
  const [shippedError, setShippedError] = useState<string | null>(null);
  const shippedReqRef = useRef(0);
  const shippedDemoModeRef = useRef(false);

  // Subscription usage is fetched independently too: the local log reader can
  // still be slower than the core snapshot on large histories, so it never
  // gates the view. The last good reading stays visible while refetching.
  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const [usageState, setUsageState] = useState<"idle" | "loading" | "error">("idle");
  const usageReqRef = useRef(0);

  // Monotonic request id so a slow background poll cannot clobber newer state.
  // A 60s poll started before the user paused an agent could otherwise resolve
  // *after* the post-pause refresh and re-show the agent as running. Each call
  // claims an id; only the latest id is allowed to commit its result.
  const reqRef = useRef(0);

  const refresh = useCallback(
    async (nextBaseUrl = baseUrl) => {
      const targetBaseUrl = clientBaseUrl(nextBaseUrl);
      const id = ++reqRef.current;
      setLoading(true);
      setError(null);
      setErrorRaw(null);
      try {
        const next = await loadSnapshot(targetBaseUrl);
        if (id !== reqRef.current) return;
        setSnapshot(next);
        setBaseUrl(targetBaseUrl);
        rememberBaseUrl(targetBaseUrl);
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

  // Board fetch, independent of the core snapshot. Keeps the last good board
  // visible while refetching (no flicker), and never throws into the view.
  const refreshShipped = useCallback(
    async (nextBaseUrl = baseUrl, options: ShippedRefreshOptions = {}) => {
      const target = clientBaseUrl(nextBaseUrl);
      const includeDemo = options.demo ?? shippedDemoModeRef.current;
      if (options.demo !== undefined) {
        shippedDemoModeRef.current = options.demo;
      }
      const id = ++shippedReqRef.current;
      setShippedState("loading");
      try {
        const board = await loadShipped(target, 14, { demo: includeDemo });
        if (id !== shippedReqRef.current) return;
        setShipped(board);
        setShippedError(null);
        setShippedState("idle");
      } catch (err) {
        if (id !== shippedReqRef.current) return;
        setShippedError(err instanceof Error ? err.message : String(err));
        setShippedState("error");
      }
    },
    [baseUrl],
  );

  // Usage fetch, independent of the core snapshot. Keeps the last good reading
  // visible while refetching, and never throws into the view: the server's
  // honest "unavailable" payload is stored as-is so the panel can explain it.
  const refreshUsage = useCallback(
    async (nextBaseUrl = baseUrl) => {
      const target = clientBaseUrl(nextBaseUrl);
      const id = ++usageReqRef.current;
      setUsageState("loading");
      try {
        const next = await loadUsage(target);
        if (id !== usageReqRef.current) return;
        setUsage(next);
        setUsageState("idle");
      } catch (err) {
        if (id !== usageReqRef.current) return;
        // A transport failure synthesizes the same "unavailable" shape the
        // server returns so the panel renders one consistent degraded state.
        setUsage({
          available: false,
          kind: "subscription",
          source: "native",
          block: null,
          codex: null,
          limits: null,
          weekly: null,
          error: err instanceof Error ? err.message : String(err),
        });
        setUsageState("error");
      }
    },
    [baseUrl],
  );

  useEffect(() => {
    void refresh();
    void refreshShipped();
    void refreshUsage();
  }, [refresh, refreshShipped, refreshUsage]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void refresh();
      void refreshShipped();
      void refreshUsage();
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh, refreshShipped, refreshUsage]);

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
        setActionNotice({ tone: "ok", message, domain: "plans" });
        await refresh(baseUrl);
      } catch (err) {
        setActionNotice({
          tone: "error",
          message: err instanceof Error ? err.message : String(err),
          domain: "plans",
        });
      } finally {
        setBusyPlanAction(null);
      }
    },
    [baseUrl, refresh],
  );

  // Record a real go/no-go on a genuine Batman plan. Approve starts that exact
  // scope; decline stops it. The server writes the marker Batman's file poll
  // watches, so this needs no Slack round-trip. Refresh after so the decided
  // plan reflects its new status and drops out of Needs-you.
  const runPlanDecision = useCallback(
    async (plan: PlanDraft, decision: PlanDecision) => {
      const key = `${plan.plan_id}:${decision}`;
      setBusyPlanAction(key);
      setActionNotice(null);
      try {
        const result = await decidePlan(baseUrl, plan.plan_id, decision);
        const target = `issue #${result.issue_number}`;
        const message =
          decision === "approve"
            ? `Approved ${target}. Batman starts this exact scope on its next run.`
            : `Declined ${target}. Batman will not start this work.`;
        setActionNotice({ tone: "ok", message, domain: "plans" });
        // Refresh both surfaces so the decision is live: the snapshot drops the
        // plan out of Needs-you, and the Work board picks up the newly queued
        // (or removed) item without a manual reload.
        await refresh(baseUrl);
        await refreshShipped(baseUrl);
      } catch (err) {
        setActionNotice({
          tone: "error",
          message: err instanceof Error ? err.message : String(err),
          domain: "plans",
        });
      } finally {
        setBusyPlanAction(null);
      }
    },
    [baseUrl, refresh, refreshShipped],
  );

  const runPlanIssueFile = useCallback(
    async (plan: PlanDraft) => {
      const key = `${plan.plan_id}:file-issue`;
      setBusyPlanAction(key);
      setActionNotice(null);
      try {
        const result = await filePlanIssue(baseUrl, plan.plan_id);
        const issue = result.issue_url || `${result.repo || "selected repo"} issue`;
        const message =
          result.status === "already_filed"
            ? `Already filed ${issue}.`
            : `Filed ${issue} with ${result.label || "agent:implement"}.`;
        setActionNotice({ tone: "ok", message, domain: "plans" });
        await refresh(baseUrl);
        await refreshShipped(baseUrl);
      } catch (err) {
        setActionNotice({
          tone: "error",
          message: err instanceof Error ? err.message : String(err),
          domain: "plans",
        });
      } finally {
        setBusyPlanAction(null);
      }
    },
    [baseUrl, refresh, refreshShipped],
  );

  const runPlanDiscard = useCallback(
    async (plan: PlanDraft) => {
      const key = `${plan.plan_id}:discard`;
      setBusyPlanAction(key);
      setActionNotice(null);
      try {
        const result = await discardPlan(baseUrl, plan.plan_id);
        const matchingDrafts =
          typeof result.discarded_count === "number" && result.discarded_count > 1
            ? result.discarded_count - 1
            : 0;
        const groupedSuffix =
          matchingDrafts > 0
            ? ` and ${matchingDrafts} matching ${matchingDrafts === 1 ? "draft" : "drafts"}`
            : "";
        const message =
          result.status === "already_discarded"
            ? `Already discarded ${plan.title}.`
            : `Discarded ${plan.title}${groupedSuffix}.`;
        setActionNotice({ tone: "ok", message, domain: "plans" });
        await refresh(baseUrl);
      } catch (err) {
        setActionNotice({
          tone: "error",
          message: err instanceof Error ? err.message : String(err),
          domain: "plans",
        });
      } finally {
        setBusyPlanAction(null);
      }
    },
    [baseUrl, refresh],
  );

  // Assign, arm, hold, or close an issue on the Kanban board. Refreshes the
  // board so the card moves to reflect the new GitHub state.
  const runQueueAction = useCallback(
    async (
      repo: string,
      issueNumber: number,
      action: QueueAction,
      targetAgent: AssignmentTargetAgent = "auto",
    ): Promise<boolean> => {
      setBusyQueue(`${action}:${repo}#${issueNumber}`);
      setActionNotice(null);
      try {
        const result = await setQueuePickup(baseUrl, repo, issueNumber, action, targetAgent);
        setActionNotice({
          tone: "ok",
          message:
            action === "queue"
              ? `Queued ${repo}#${issueNumber} for pickup.`
              : action === "assign"
                ? assignmentNoticeMessage(repo, issueNumber, targetAgent, result)
              : action === "done"
                ? `Closed ${repo}#${issueNumber} as done.`
                : `Held ${repo}#${issueNumber} so no agent picks it up.`,
          domain: "board",
        });
        await refreshShipped();
        return true;
      } catch (err) {
        setActionNotice({
          tone: "error",
          message: err instanceof Error ? err.message : String(err),
          domain: "board",
        });
        return false;
      } finally {
        setBusyQueue(null);
      }
    },
    [baseUrl, refreshShipped],
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
          domain: "setup",
        });
        await refresh(baseUrl);
      } catch (err) {
        setActionNotice({
          tone: "error",
          message: err instanceof Error ? err.message : String(err),
          domain: "setup",
        });
      } finally {
        setBusyTrustedUser(null);
      }
    },
    [baseUrl, refresh],
  );

  const runMemoryCandidateAction = useCallback(
    async (candidateId: string, action: "promote" | "reject") => {
      // One memory action at a time. The cards only disable their own buttons,
      // so this guards against a click on another card while one is in flight.
      // The ref is set synchronously, so even two clicks in the same tick can't
      // both pass.
      if (busyMemoryRef.current) {
        return;
      }
      busyMemoryRef.current = true;
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
          domain: "memory",
        });
        await refresh(baseUrl);
      } catch (err) {
        setActionNotice({
          tone: "error",
          message: err instanceof Error ? err.message : String(err),
          domain: "memory",
        });
      } finally {
        busyMemoryRef.current = false;
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
          domain: "setup",
        });
        await refresh(baseUrl);
      } catch (err) {
        setActionNotice({
          tone: "error",
          message: err instanceof Error ? err.message : String(err),
          domain: "setup",
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
    async ({
      action,
      target,
      cadence,
      refreshAfter = false,
    }: NativeActionRequest): Promise<NativeCommandResult | null> => {
      const key = `${action}:${target || "fleet"}`;
      setNativeBusy(key);
      setNativeError(null);
      setNativeErrorRaw(null);
      setNativeResult(null);
      try {
        const result = await runNativeAction(action, target, cadence);
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
        return result;
      } catch (err) {
        setNativeError(err instanceof Error ? err.message : String(err));
        setNativeErrorRaw(errorDetail(err));
        return null;
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
      window.setTimeout(() => void refresh(baseUrl), 900);
    } catch (err) {
      setNativeError(err instanceof Error ? err.message : String(err));
      setNativeErrorRaw(errorDetail(err));
    } finally {
      setNativeBusy(null);
    }
  }, [baseUrl, refresh]);

  const installCore = useCallback(async () => {
    setNativeBusy("core:install");
    setNativeError(null);
    setNativeErrorRaw(null);
    setNativeResult(null);
    try {
      const result = await installAlfredCore();
      setNativeResult(result);
      if (!result.success) {
        return;
      }
      setNativeBusy("runtime:start");
      const runtime = await startLocalRuntime();
      setNativeResult({
        ...runtime,
        message: runtime.success
          ? "Alfred core installed and the local runtime started."
          : runtime.message || "Alfred core installed, but the local runtime did not start.",
      });
      window.setTimeout(() => void refresh(baseUrl), 900);
    } catch (err) {
      setNativeError(err instanceof Error ? err.message : String(err));
      setNativeErrorRaw(errorDetail(err));
    } finally {
      setNativeBusy(null);
    }
  }, [baseUrl, refresh]);

  const clearNativeResult = useCallback(() => {
    setNativeResult(null);
    setNativeError(null);
    setNativeErrorRaw(null);
  }, []);

  // A successful action result should not linger forever pinned to the top of
  // the app. Auto-clear it after a few seconds (errors stay until dismissed so
  // they are not missed); the operator can also dismiss it immediately.
  useEffect(() => {
    if (!nativeResult || nativeError || nativeResult.success === false) return;
    const timer = window.setTimeout(() => setNativeResult(null), 12_000);
    return () => window.clearTimeout(timer);
  }, [nativeResult, nativeError]);

  // An inline action notice should not linger forever on its surface either.
  // Auto-clear a successful notice after a few seconds (errors stay until the
  // next action replaces them so a failure is not missed), mirroring the
  // nativeResult timer above.
  useEffect(() => {
    if (!actionNotice || actionNotice.tone !== "ok") return;
    const timer = window.setTimeout(() => setActionNotice(null), 12_000);
    return () => window.clearTimeout(timer);
  }, [actionNotice]);

  // A notice is scoped to the surface that raised it, so a banner from one
  // surface (e.g. promoting a lesson on Memory) never bleeds onto another
  // (Plans / Board / Setup). Each surface reads its own slice via this helper.
  const noticeFor = useCallback(
    (domain: NoticeDomain): ActionNotice =>
      actionNotice && actionNotice.domain === domain ? actionNotice : null,
    [actionNotice],
  );

  // The calm "needs you" decisions (plan sign-off + memory review) drive
  // Review's home lane; reliability inspection signals are operator depth and
  // surface only in the Operator drawer.
  const needsYou = useMemo(() => buildNeedsYou(snapshot), [snapshot]);

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
    snapshot,
    error,
    errorRaw,
    loading,
    busyPlanAction,
    busyMemoryAction,
    busyTrustedUser,
    busyQueue,
    noticeFor,
    nativeBusy,
    nativeResult,
    nativeError,
    nativeErrorRaw,
    clearNativeResult,
    needsYou,
    fleetService,
    fleetRows,
    fleetHealth,
    feed,
    unseenCount,
    seenIds,
    markActivitySeen,
    shipped,
    shippedState,
    shippedError,
    refreshShipped,
    usage,
    usageState,
    refreshUsage,
    refresh,
    refreshFleetService,
    runFollowupAction,
    runPlanDecision,
    runPlanDiscard,
    runPlanIssueFile,
    runQueueAction,
    runMemoryCandidateAction,
    addTrustedUser,
    removeTrustedUser,
    runLocalAction,
    installCore,
    startRuntime,
  };
}
