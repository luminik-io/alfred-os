import { AlertTriangle, Bell, CheckCircle2, ChevronRight, Radio } from "lucide-react";

import { friendlyTime } from "../format";
import type { FeedItem, FeedTarget } from "../lib/notifications";
import { EmptyState, PanelHeader } from "./atoms";

export function NotificationsView({
  feed,
  unseen,
  seen,
  onMarkAllSeen,
  onOpenTarget,
  embedded = false,
}: {
  feed: FeedItem[];
  unseen: number;
  seen: Set<string>;
  onMarkAllSeen: () => void;
  /** Follow a feed row to its source: an agent's latest run or the Lessons queue. */
  onOpenTarget?: (target: FeedTarget) => void;
  /** When embedded inside another panel (e.g. the Logs tabs) drop the
   *  panel chrome + header so it does not nest a card inside a card. */
  embedded?: boolean;
}) {
  const body = (
    <>
      <p className="panel-intro">
        Recent firings and governor &ldquo;needs you&rdquo; items collect here instead of macOS
        banners. This is the surface to read fleet activity from.
      </p>
      {feed.length ? (
        <ol className="feed-list">
          {feed.map((item) => (
            <FeedRow key={item.id} item={item} isNew={!seen.has(item.id)} onOpen={onOpenTarget} />
          ))}
        </ol>
      ) : (
        <EmptyState
          title="No activity yet."
          body="Once agents fire or the governor flags a needs-you item, it appears here newest-first."
        />
      )}
    </>
  );

  if (embedded) {
    return body;
  }

  return (
    <section className="panel">
      <PanelHeader
        eyebrow="Activity"
        title={unseen ? `Notification center (${unseen} new)` : "Notification center"}
        actionLabel={unseen ? "Mark all read" : undefined}
        onAction={unseen ? onMarkAllSeen : undefined}
      />
      {body}
    </section>
  );
}

function FeedRow({
  item,
  isNew,
  onOpen,
}: {
  item: FeedItem;
  isNew: boolean;
  onOpen?: (target: FeedTarget) => void;
}) {
  const Icon = iconFor(item);
  const clickable = Boolean(item.target && onOpen);
  const content = (
    <>
      <Icon size={18} aria-hidden="true" />
      <div className="feed-item__body">
        <div className="feed-item__head">
          <strong>{item.title}</strong>
          {isNew ? <span className="feed-item__pip" aria-label="Unread" /> : null}
        </div>
        <p>{item.detail}</p>
      </div>
      <span className="feed-item__side">
        <time className="feed-item__time">{item.at ? friendlyTime(item.at) : "now"}</time>
        {clickable ? <ChevronRight className="feed-item__chevron" size={15} aria-hidden="true" /> : null}
      </span>
    </>
  );
  const toneClass = `feed-item feed-item--${item.tone}${isNew ? " feed-item--new" : ""}`;
  if (clickable) {
    return (
      <li className={`${toneClass} feed-item--clickable`}>
        <button
          type="button"
          className="feed-item__row"
          onClick={() => onOpen!(item.target!)}
          aria-label={openLabel(item)}
        >
          {content}
        </button>
      </li>
    );
  }
  return (
    <li className={toneClass}>
      <div className="feed-item__row">{content}</div>
    </li>
  );
}

function openLabel(item: FeedItem): string {
  if (item.target?.type === "memory") return `Review lesson suggestions: ${item.title}`;
  if (item.target?.type === "agent") return `Open ${item.target.codename}'s latest run: ${item.title}`;
  return item.title;
}

function iconFor(item: FeedItem) {
  if (item.kind === "needs-you") {
    return item.tone === "error" ? AlertTriangle : Bell;
  }
  if (item.tone === "error") return AlertTriangle;
  if (item.tone === "ok") return CheckCircle2;
  return Radio;
}
