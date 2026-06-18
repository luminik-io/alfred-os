import { ArrowRight, MessageCircle, UserPlus, Users } from "lucide-react";
import { useEffect, useState } from "react";

import {
  addTrustedSlackUser,
  errorDetail,
  loadTrustedSlackUsers,
} from "../../api";
import type { TrustedSlackUser } from "../../types";
import { Button, Card, CardContent, Input, Label } from "../ui";
import type { OnboardingNotice } from "./types";

/**
 * Step 4: Connect Slack (optional, clearly skippable). Skip is a first-class
 * button, not a tiny link. Maya skips. A Dev who wants approvals and questions
 * in Slack adds a trusted approver (POST /api/slack/trusted-users).
 */
export function SlackStep({
  baseUrl,
  connected,
  canMutate,
  onSkip,
  onApproverAdded,
  setNotice,
}: {
  baseUrl: string;
  connected: boolean;
  canMutate: boolean;
  onSkip: () => void;
  // Called once an approver is added so the orchestrator marks the step done.
  onApproverAdded: () => void;
  setNotice: (notice: OnboardingNotice) => void;
}) {
  const [users, setUsers] = useState<TrustedSlackUser[]>([]);
  const [userId, setUserId] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!connected) return;
    let cancelled = false;
    loadTrustedSlackUsers(baseUrl)
      .then((result) => {
        if (!cancelled) setUsers(result.users || []);
      })
      .catch(() => {
        // A missing list is not fatal here; the step still lets Dev add one.
      });
    return () => {
      cancelled = true;
    };
  }, [baseUrl, connected]);

  const add = async () => {
    const trimmed = userId.trim();
    if (!trimmed) return;
    setBusy(true);
    try {
      const result = await addTrustedSlackUser(baseUrl, trimmed);
      setUsers(result.users || []);
      setUserId("");
      onApproverAdded();
      setNotice({ tone: "ok", message: `Added ${trimmed} as a Slack approver.` });
    } catch (err) {
      setNotice({ tone: "error", message: errorDetail(err) || "Could not add that Slack user." });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid gap-4">
      <p className="text-sm text-muted-foreground">
        Want approvals and questions in Slack too? Alfred can ping a trusted person there when a plan
        needs a go-ahead. This is optional. You can add it later from Settings.
      </p>

      <div className="flex flex-wrap gap-2">
        <Button type="button" onClick={onSkip}>
          <span>Skip for now</span>
          <ArrowRight size={16} aria-hidden="true" />
        </Button>
      </div>

      <Card size="sm" className="rounded-lg border-border/70 bg-background/55 shadow-none">
        <CardContent className="px-3">
          <details className="group grid gap-2">
            <summary className="cursor-pointer list-none">
              <span className="grid gap-0.5">
                <strong className="flex items-center gap-2 text-sm font-medium">
                  <MessageCircle size={14} aria-hidden="true" />
                  Add a Slack approver
                </strong>
                <span className="text-xs text-muted-foreground">
                  Trust a Slack user so they can discuss plans and request drafts.
                </span>
              </span>
            </summary>

            <form
              className="mt-3 grid gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                void add();
              }}
            >
              <Label htmlFor="onboarding-slack-user">Slack user ID</Label>
              <div className="grid gap-2 md:grid-cols-[1fr_auto]">
                <Input
                  id="onboarding-slack-user"
                  value={userId}
                  onChange={(event) => setUserId(event.currentTarget.value)}
                  placeholder="U0123ABCDEF"
                  spellCheck={false}
                />
                <Button type="submit" disabled={!canMutate || busy || !userId.trim()}>
                  <UserPlus size={15} aria-hidden="true" />
                  <span>{busy ? "Adding" : "Trust"}</span>
                </Button>
              </div>
            </form>

            {users.length ? (
              <ul className="mt-3 grid gap-2" aria-label="Trusted Slack approvers">
                {users.map((user) => (
                  <li
                    key={user.user_id}
                    className="flex items-center gap-2 rounded-md border border-border/60 bg-card/60 px-2.5 py-2 text-sm"
                  >
                    <Users size={14} aria-hidden="true" className="text-muted-foreground" />
                    <span className="font-mono text-xs">{user.user_id}</span>
                    <span className="ml-auto text-xs text-muted-foreground">
                      {user.sources.join(", ")}
                    </span>
                  </li>
                ))}
              </ul>
            ) : null}

            {!canMutate ? (
              <p className="mt-3 rounded-lg border border-border/70 bg-muted/35 px-3 py-2 text-sm text-muted-foreground">
                The desktop app adds Slack approvers. The browser preview is read-only here.
              </p>
            ) : null}
          </details>
        </CardContent>
      </Card>
    </div>
  );
}
