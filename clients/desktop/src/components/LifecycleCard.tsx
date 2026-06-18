import type { ReactNode } from "react";

import { exactTime, friendlyTime } from "../format";
import { chipToneClass, type Chip } from "../lib/chips";

// The canonical lifecycle card (DESIGN_SPEC section 5, "Card anatomy"). Every
// Request, Plan, Run, and Shipped item renders as this single component:
//
//   [status chip] [repo chip] [+N]            [age]   <- meta row
//   Outcome sentence in plain words, one line.        <- the outcome (title)
//   [agent] attribution                  [primary action]
//
// Rules enforced here, not by callers:
//   - exactly one status chip, carrying the only status color on the card;
//   - at most two repo chips, then a +N overflow with the full list on hover;
//   - one primary action; secondary actions live in a detail panel, not here;
//   - no empty card: a card with no outcome and no action is not rendered by
//     callers (they use an empty-state pattern instead).

export type RepoChip = {
  short: string;
  full: string;
};

export function AlfredChip({ chip }: { chip: Chip }) {
  return (
    <span className={`alfred-chip ${chipToneClass(chip.tone)}`}>
      <span className="alfred-chip__dot" aria-hidden="true" />
      {chip.label}
    </span>
  );
}

function RepoChips({ repos }: { repos: RepoChip[] }) {
  if (!repos.length) return null;
  const visible = repos.slice(0, 2);
  const overflow = repos.slice(2);
  return (
    <span className="alfred-card__repos">
      {visible.map((repo) => (
        <span key={repo.full} className="alfred-repo-chip" title={repo.full}>
          {repo.short}
        </span>
      ))}
      {overflow.length ? (
        <span
          className="alfred-repo-chip alfred-repo-chip--overflow"
          title={overflow.map((repo) => repo.full).join(", ")}
        >
          +{overflow.length}
        </span>
      ) : null}
    </span>
  );
}

export function LifecycleCard({
  chip,
  repos = [],
  age,
  outcome,
  attribution,
  action,
  selected = false,
  onSelect,
  ariaLabel,
}: {
  chip: Chip;
  repos?: RepoChip[];
  // ISO timestamp; rendered as a friendly relative age with the exact time on
  // hover. Omit when no timestamp is known.
  age?: string | null;
  outcome: string;
  // Agent attribution ("Lucius"), small and muted. Optional.
  attribution?: ReactNode;
  // The single primary action for this card. Optional (e.g. read-only cards).
  action?: ReactNode;
  selected?: boolean;
  // Selecting the card opens its detail panel. There is no separate Inspect
  // verb (issue 314): the card body is the selection target.
  onSelect?: () => void;
  ariaLabel?: string;
}) {
  const body = (
    <>
      <div className="alfred-card__meta">
        <AlfredChip chip={chip} />
        <RepoChips repos={repos} />
        {age ? (
          <time className="alfred-card__age" title={exactTime(age)}>
            {friendlyTime(age)}
          </time>
        ) : null}
      </div>
      <p className="alfred-card__outcome">{outcome}</p>
    </>
  );
  const classes = [
    "alfred-card",
    onSelect ? "alfred-card--interactive" : null,
    selected ? "alfred-card--selected" : null,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <article className={classes} aria-label={ariaLabel}>
      {onSelect ? (
        <button
          type="button"
          className="alfred-card__select"
          onClick={onSelect}
          aria-pressed={selected}
        >
          {body}
        </button>
      ) : (
        <div className="alfred-card__static">{body}</div>
      )}
      {attribution || action ? (
        <div className="alfred-card__foot">
          <span className="alfred-card__attribution">{attribution}</span>
          {action ? <div className="alfred-card__action">{action}</div> : null}
        </div>
      ) : null}
    </article>
  );
}
