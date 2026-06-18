import { ArrowRight, PlayCircle, Sparkles, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import {
  clearSetupDemo,
  composeSetupPlaybook,
  errorDetail,
  loadSetupPlaybooks,
  seedSetupDemo,
} from "../../api";
import type { TabKey } from "../../lib/uiTypes";
import type { SetupPlaybook } from "../../types";
import { Button, Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "../ui";
import type { OnboardingNotice } from "./types";

/**
 * Step 5: First request (the payoff). The journey always ends on a populated
 * Inbox, never an empty one.
 *
 *  - Maya path: starter specs as plain cards (GET /api/setup/playbooks).
 *    Selecting one drafts a real first Request (POST /api/setup/playbook) and
 *    lands her on Ask to refine it in plain words.
 *  - Demo path: "Show me a sample first" seeds a labelled demo lifecycle
 *    (POST /api/setup/demo) so Home / Pipeline render populated and clearly
 *    "Sample". The step then keeps a "Clear sample data" control
 *    (POST /api/setup/demo/clear) next to "Open Home" so the sample is never a
 *    one-way door.
 *  - Dev shortcut: skip straight to writing a brief in Ask.
 */
export function FirstRequestStep({
  baseUrl,
  canMutate,
  reposReady,
  demoPresent,
  setNotice,
  onSwitch,
  onComplete,
  onSeedDemo,
  onClearDemo,
}: {
  baseUrl: string;
  canMutate: boolean;
  reposReady: boolean;
  // Server truth from SetupStatus.demo.present, so the "Clear sample data"
  // exit survives a remount (open Inbox, reload, navigate back) instead of
  // depending only on the in-component seed flag, which resets to false.
  demoPresent: boolean;
  setNotice: (notice: OnboardingNotice) => void;
  onSwitch?: (tab: TabKey) => void;
  // Called after a real request or demo lands so the orchestrator can mark the
  // journey complete and refresh the board.
  onComplete: (kind: "request" | "demo") => void;
  // Seed the demo lifecycle and refresh the board, owned by the orchestrator so
  // it can also flip the board into demo mode.
  onSeedDemo: () => Promise<void>;
  // Flip the board back out of demo mode after the sample is cleared, owned by
  // the orchestrator so it can also refresh the board with demo: false.
  onClearDemo: () => Promise<void>;
}) {
  const [playbooks, setPlaybooks] = useState<SetupPlaybook[]>([]);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [demoBusy, setDemoBusy] = useState(false);
  // Optimistic local override so the seed/clear toggle flips instantly, before
  // the parent's status refresh resolves. null means "defer to server truth"
  // (demoPresent); true/false is an in-flight optimistic value. This keeps the
  // "Clear sample data" exit visible across a remount, because demoPresent is
  // sourced from SetupStatus.demo.present rather than a flag that resets.
  const [demoSeededOverride, setDemoSeededOverride] = useState<boolean | null>(null);
  const demoSeeded = demoSeededOverride ?? demoPresent;
  const [clearBusy, setClearBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Once the server confirms a state that matches the optimistic override,
  // drop the override so the component tracks server truth again.
  useEffect(() => {
    if (demoSeededOverride !== null && demoSeededOverride === demoPresent) {
      setDemoSeededOverride(null);
    }
  }, [demoSeededOverride, demoPresent]);

  useEffect(() => {
    let cancelled = false;
    loadSetupPlaybooks(baseUrl)
      .then((result) => {
        if (!cancelled) setPlaybooks(result.playbooks);
      })
      .catch((err) => {
        if (!cancelled) setError(errorDetail(err) || "Could not load starter specs.");
      });
    return () => {
      cancelled = true;
    };
  }, [baseUrl]);

  const pick = async (key: string) => {
    setBusyKey(key);
    try {
      const result = await composeSetupPlaybook(baseUrl, key);
      setNotice({
        tone: "ok",
        message: `Drafted your first request: "${result.title}". Refine it in Ask, then save the plan.`,
      });
      onComplete("request");
      onSwitch?.("compose");
    } catch (err) {
      setNotice({ tone: "error", message: errorDetail(err) || "Could not draft from that spec." });
    } finally {
      setBusyKey(null);
    }
  };

  const seedDemo = async () => {
    setDemoBusy(true);
    try {
      await seedSetupDemo(baseUrl);
      setDemoSeededOverride(true);
      await onSeedDemo();
      setNotice({
        tone: "ok",
        message:
          "Seeded a sample lifecycle, clearly labelled. Inbox and Work are populated. Clear it whenever you like.",
      });
      onComplete("demo");
    } catch (err) {
      setNotice({ tone: "error", message: errorDetail(err) || "Could not seed the sample." });
    } finally {
      setDemoBusy(false);
    }
  };

  const clearDemo = async () => {
    setClearBusy(true);
    try {
      await clearSetupDemo(baseUrl);
      setDemoSeededOverride(false);
      await onClearDemo();
      setNotice({
        tone: "ok",
        message: "Cleared the sample data. Inbox and Work are back to your real work.",
      });
    } catch (err) {
      setNotice({ tone: "error", message: errorDetail(err) || "Could not clear the sample." });
    } finally {
      setClearBusy(false);
    }
  };

  return (
    <div className="grid gap-4">
      {error ? (
        <Card size="sm" className="rounded-lg border-border/70 bg-muted/35 shadow-none">
          <CardContent className="px-3 text-sm text-muted-foreground">{error}</CardContent>
        </Card>
      ) : null}

      <div className="grid gap-2">
        <p className="text-sm font-medium text-foreground">Pick something for Alfred to do first</p>
        {!reposReady ? (
          <p className="text-sm text-muted-foreground">
            Choosing a repository first lets these run on your real project. You can still seed a
            sample below without one.
          </p>
        ) : null}
        {playbooks.map((playbook) => (
          <Card
            size="sm"
            className="rounded-lg border-border/70 bg-background/55 shadow-none"
            key={playbook.key}
          >
            <CardHeader className="gap-2 md:grid-cols-[1fr_auto]">
              <div className="min-w-0">
                <CardTitle className="text-sm">{playbook.title}</CardTitle>
                <CardDescription>{playbook.summary}</CardDescription>
              </div>
              <CardAction>
                <Button
                  variant="outline"
                  type="button"
                  onClick={() => void pick(playbook.key)}
                  disabled={!canMutate || busyKey !== null}
                >
                  <Sparkles size={14} aria-hidden="true" />
                  <span>{busyKey === playbook.key ? "Drafting" : "Use this"}</span>
                </Button>
              </CardAction>
            </CardHeader>
          </Card>
        ))}
        {playbooks.length === 0 && !error ? (
          <p className="text-sm text-muted-foreground">Loading starter specs.</p>
        ) : null}
      </div>

      <Card size="sm" className="rounded-lg border-border/70 bg-muted/25 shadow-none">
        <CardContent className="grid gap-2 px-3">
          <p className="text-sm font-medium text-foreground">Just want to look first?</p>
          {demoSeeded ? (
            <>
              <p className="text-sm text-muted-foreground">
                Sample data is active. Inbox, Work, and shipped outcomes are populated and clearly labelled
                "Sample". Clear it whenever you want your real board back.
              </p>
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  type="button"
                  onClick={() => void clearDemo()}
                  disabled={!canMutate || clearBusy}
                >
                  <Trash2 size={15} aria-hidden="true" />
                  <span>{clearBusy ? "Clearing" : "Clear sample data"}</span>
                </Button>
                <Button variant="ghost" type="button" onClick={() => onSwitch?.("home")}>
                  <span>Open Inbox</span>
                  <ArrowRight size={15} aria-hidden="true" />
                </Button>
              </div>
            </>
          ) : (
            <>
              <p className="text-sm text-muted-foreground">
                Seed a sample lifecycle so Inbox, Work, and shipped outcomes render populated and clearly
                labelled "Sample". Clear it any time.
              </p>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  onClick={() => void seedDemo()}
                  disabled={!canMutate || demoBusy}
                >
                  <PlayCircle size={15} aria-hidden="true" />
                  <span>{demoBusy ? "Seeding" : "Show me a sample first"}</span>
                </Button>
                <Button variant="ghost" type="button" onClick={() => onSwitch?.("compose")}>
                  <span>Write a brief in Ask</span>
                  <ArrowRight size={15} aria-hidden="true" />
                </Button>
              </div>
            </>
          )}
        </CardContent>
      </Card>

      {!canMutate ? (
        <p className="rounded-lg border border-border/70 bg-muted/35 px-3 py-2 text-sm text-muted-foreground">
          The desktop app drafts a first request and seeds the sample. The browser preview can read
          the starter specs but cannot draft or seed.
        </p>
      ) : null}
    </div>
  );
}
