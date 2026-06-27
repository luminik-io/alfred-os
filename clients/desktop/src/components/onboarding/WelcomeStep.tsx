import { ArrowRight, KeyRound, Server, Sparkles } from "lucide-react";

import type { SetupInstallInventory } from "../../types";
import { InstallInventoryPanel } from "./InstallInventoryPanel";
import { Button } from "../ui";

/**
 * Step 0: Welcome. The hero screen of the first-run takeover. It says the value
 * once, then leads with the trust differentiator (no API keys, runs on the
 * subscriptions you already pay for), and offers one primary door for the guided
 * path plus a quiet shortcut for a developer who already has a server running.
 *
 * It deliberately does not repeat the shell header ("Let's connect Alfred, seven
 * short steps") or carry a StepFrame title above it: the journey framing is said
 * once in the shell, and the value is said once here.
 */
export function WelcomeStep({
  install,
  onGetStarted,
  onDevShortcut,
}: {
  install?: SetupInstallInventory | null;
  onGetStarted: () => void;
  onDevShortcut: () => void;
}) {
  return (
    <div className="alfred-onboarding-welcome grid gap-7 text-center">
      <div className="mx-auto grid max-w-xl gap-4">
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
          Alfred opens pull requests, handles reviews, and reports back in Slack,
          all on your own machine while you stay in control.
        </p>
      </div>

      <div className="alfred-onboarding-welcome__trust">
        <KeyRound size={15} aria-hidden="true" />
        <span>
          No API keys. Alfred runs on the Claude Max and Codex Pro subscriptions
          you already pay for.
        </span>
      </div>

      <InstallInventoryPanel inventory={install} />

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
    </div>
  );
}
