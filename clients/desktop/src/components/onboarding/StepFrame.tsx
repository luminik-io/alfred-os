import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { Badge } from "../ui";

/**
 * The header every onboarding step renders above its body. Keeps the icon,
 * title, blurb, and optional accent label consistent across the steps so the
 * takeover reads as one journey, not a pile of different screens.
 *
 * The frame itself is chromeless now: the glass shell is owned by OnboardingView
 * so the whole stepper is one floating surface over the ambient base, and each
 * step body just renders its content beneath this header. Generous whitespace,
 * one decision per step, minimal noise.
 */
export function StepFrame({
  icon: Icon,
  title,
  blurb,
  accentLabel,
  children,
}: {
  icon: LucideIcon;
  title: string;
  blurb: string;
  accentLabel?: string;
  children?: ReactNode;
}) {
  return (
    <div className="alfred-step">
      <header className="alfred-step__head">
        <span className="alfred-step__icon" aria-hidden="true">
          <Icon size={18} />
        </span>
        <div className="min-w-0">
          <h2 className="alfred-step__title">
            <span>{title}</span>
            {accentLabel ? (
              <Badge variant="outline" className="font-normal">
                {accentLabel}
              </Badge>
            ) : null}
          </h2>
          <p className="alfred-step__blurb">{blurb}</p>
        </div>
      </header>
      <div className="alfred-step__body">{children}</div>
    </div>
  );
}
