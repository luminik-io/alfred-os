import { ArrowRight, Server, Sparkles } from "lucide-react";

import { Button } from "../ui";

/**
 * Step 0: Welcome. One near-black screen that teaches the mental model in a
 * line and offers two doors: Maya's "Get started" and Dev's "I have a server
 * running" (jumps straight to the GitHub / paste-URL step). No data fetch.
 */
export function WelcomeStep({
  onGetStarted,
  onDevShortcut,
}: {
  onGetStarted: () => void;
  onDevShortcut: () => void;
}) {
  return (
    <div className="alfred-onboarding-welcome grid gap-6 text-center">
      <div className="mx-auto grid max-w-xl gap-3">
        <span
          className="mx-auto flex size-12 items-center justify-center rounded-full border border-primary/30 bg-primary/15 text-primary"
          aria-hidden="true"
        >
          <Sparkles size={22} />
        </span>
        <h2 className="font-heading text-3xl font-medium tracking-tight text-foreground md:text-4xl">
          Wake up to shipped work you can trust.
        </h2>
        <p className="text-base text-muted-foreground">
          Alfred runs your own Claude Code and Codex to open pull requests, handle reviews, and
          report back in Slack. Let's connect it. About two minutes.
        </p>
      </div>
      <div className="mx-auto flex flex-wrap items-center justify-center gap-2">
        <Button type="button" size="lg" onClick={onGetStarted}>
          <span>Get started</span>
          <ArrowRight size={16} aria-hidden="true" />
        </Button>
        <Button type="button" variant="ghost" size="lg" onClick={onDevShortcut}>
          <Server size={16} aria-hidden="true" />
          <span>I have a server running</span>
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        No API keys, no cloud dashboard, no token paste. Everything runs on this Mac.
      </p>
    </div>
  );
}
