import { Check, History } from "lucide-react";
import { Popover } from "radix-ui";

import { friendlyTime } from "../../format";
import type { RecentThread } from "./useAskThread";

// A lightweight recent-threads switcher: resume any of the last 5 local Ask
// conversations. The durable artifacts (issues/specs) remain the real output;
// this is convenience history only. Rendered as a quiet ghost button that opens
// a popover list. Hidden entirely when there are no other threads to switch to.
export function RecentThreads({
  threads,
  onResume,
}: {
  threads: RecentThread[];
  onResume: (id: string) => void;
}) {
  // Nothing to switch to until there is more than the active thread.
  if (threads.length <= 1) return null;

  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button type="button" className="ghost-button ask__recent-trigger">
          <History size={14} aria-hidden="true" />
          <span>Recent</span>
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className="ask__recent-menu"
          align="end"
          sideOffset={6}
          aria-label="Recent Ask conversations"
        >
          <p className="ask__recent-title">Recent chats</p>
          <ul className="ask__recent-list">
            {threads.map((thread) => (
              <li key={thread.id}>
                <Popover.Close asChild>
                  <button
                    type="button"
                    className={`ask__recent-item${thread.active ? " ask__recent-item--active" : ""}`}
                    onClick={() => onResume(thread.id)}
                    aria-current={thread.active ? "true" : undefined}
                  >
                    <span className="ask__recent-item-text">{thread.title}</span>
                    <span className="ask__recent-item-meta">
                      {thread.active ? (
                        <Check size={13} aria-label="Current chat" />
                      ) : (
                        friendlyTime(new Date(thread.updatedAt).toISOString())
                      )}
                    </span>
                  </button>
                </Popover.Close>
              </li>
            ))}
          </ul>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
