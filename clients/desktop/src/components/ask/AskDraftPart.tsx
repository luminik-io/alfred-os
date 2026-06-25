import { CheckCircle2, ExternalLink } from "lucide-react";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";

import { repoShortName } from "../../lib/chips";
import { openExternal } from "../../lib/links";
import { LifecycleCard, type RepoChip } from "../LifecycleCard";
import { useAskSurface } from "./AskContext";
import { cleanRepos, type DraftCardModel } from "./askModel";
import type { DraftToolArgs } from "./useAskThread";

function repoChipsFor(repos: string[]): RepoChip[] {
  return cleanRepos(repos).map((repo) => ({ short: repoShortName(repo), full: repo }));
}

// The inline lifecycle card rendered when a turn produces a saved draft, wired
// as an assistant-ui "alfred-draft" tool-call part. One primary action: File
// issue (one-gate). The card is an OFFER attached to a build turn, not a form:
// the chat reply already carries Alfred's questions, so the card stays quiet
// (neutral "Draft plan") until the plan is ready to file ("Ready to file"), and
// File issue is always available because the server is the real readiness gate.
export function AskDraftPart({ args, toolCallId }: ToolCallMessagePartProps<DraftToolArgs>) {
  const surface = useAskSurface();
  const draft: DraftCardModel | undefined = args?.draft;
  if (!draft) return null;

  // The file notice rides only the most recent draft card so a filed
  // confirmation stays visible when conversational turns ("thanks") follow.
  const notice = toolCallId === surface.lastDraftToolCallId ? surface.fileNotice : null;
  const filed = notice?.tone === "ok";

  return (
    <div className="ask-draft" aria-label="Plan Alfred is shaping">
      <LifecycleCard
        chip={
          filed
            ? { label: "Filed", tone: "ok" }
            : draft.ready
              ? { label: "Ready to file", tone: "ok" }
              : { label: "Draft plan", tone: "idle" }
        }
        repos={repoChipsFor(draft.repos)}
        outcome={draft.title}
        attribution={
          <span>{draft.ready ? "Ready when you are" : "Keep chatting to firm it up"}</span>
        }
        action={
          filed ? (
            notice?.url ? (
              <button
                className="secondary-button"
                type="button"
                onClick={() => void openExternal(notice.url as string)}
              >
                <ExternalLink size={15} aria-hidden="true" />
                <span>View issue</span>
              </button>
            ) : (
              <button className="secondary-button" type="button" onClick={surface.onOpenWork}>
                <span>Open Work</span>
              </button>
            )
          ) : (
            <button
              className={
                draft.ready ? "icon-button ask-draft__file" : "secondary-button ask-draft__file"
              }
              type="button"
              disabled={surface.fileBusy}
              onClick={() => surface.onFile(draft.draftId)}
              title={
                draft.ready
                  ? "File this as a GitHub issue"
                  : "File it now, or keep chatting to add detail first"
              }
            >
              <CheckCircle2 size={15} aria-hidden="true" />
              <span>
                {surface.fileBusy ? "Filing..." : draft.ready ? "File issue" : "File as an issue"}
              </span>
            </button>
          )
        }
        ariaLabel={`Plan: ${draft.title}`}
      />
      {notice ? (
        <p className={`ask-draft__notice ask-draft__notice--${notice.tone}`} role="status">
          {notice.message}
        </p>
      ) : null}
    </div>
  );
}
