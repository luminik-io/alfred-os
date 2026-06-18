import { Check } from "lucide-react";

import type { OnboardingStepKey, StepProgress } from "./types";
import { cn } from "@/lib/utils";

/**
 * The persistent, minimal progress indicator for the onboarding takeover. A
 * single horizontal row of numbered nodes (current / done / upcoming) joined by
 * connectors, so the user always sees where they are and how far is left without
 * the rail competing with the step content for the page.
 *
 * It is the one chrome element of the stepper: numbers and a thin connector,
 * no decorative noise. On phone widths the labels collapse to the active step's
 * name only (the dots stay) so the row never wraps or truncates.
 */

export type StepperItem = {
  key: OnboardingStepKey;
  /** Short rail title, e.g. "GitHub". */
  label: string;
  /** done / active / todo, derived from real readiness by the orchestrator. */
  state: StepProgress;
  /** Optional steps (Slack) carry a quiet marker. */
  optional?: boolean;
};

export function Stepper({
  steps,
  activeKey,
  onSelect,
}: {
  steps: StepperItem[];
  activeKey: OnboardingStepKey;
  onSelect: (key: OnboardingStepKey) => void;
}) {
  const total = steps.length;
  const activeIndex = steps.findIndex((step) => step.key === activeKey);
  const completed = steps.filter((step) => step.state === "done").length;

  return (
    <nav
      className="alfred-stepper"
      aria-label="Onboarding progress"
      data-active-index={activeIndex}
    >
      <ol className="alfred-stepper__track" role="list">
        {steps.map((step, index) => {
          const isActive = step.key === activeKey;
          const isDone = step.state === "done";
          return (
            <li
              key={step.key}
              className={cn(
                "alfred-stepper__step",
                isActive && "is-active",
                isDone && "is-done",
              )}
            >
              {index > 0 ? (
                <span
                  className={cn(
                    "alfred-stepper__connector",
                    steps[index - 1].state === "done" && "is-filled",
                  )}
                  aria-hidden="true"
                />
              ) : null}
              <button
                type="button"
                className="alfred-stepper__node"
                onClick={() => onSelect(step.key)}
                aria-current={isActive ? "step" : undefined}
                // The accessible name is the bare step label so the rail stays
                // queryable by title; position and status ride on the visually
                // hidden suffix and aria-current, never overwriting the name.
                aria-label={step.label}
              >
                <span className="alfred-stepper__dot" aria-hidden="true">
                  {isDone ? <Check size={13} strokeWidth={2.5} /> : index + 1}
                </span>
                <span
                  className={cn(
                    "alfred-stepper__label",
                    isActive ? "is-visible" : "is-collapsible",
                  )}
                >
                  {step.label}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
      <p
        className="alfred-stepper__count"
        aria-live="polite"
        aria-label={`${completed} of ${total} onboarding steps complete`}
      >
        {completed} of {total} done
      </p>
    </nav>
  );
}
