import type { LucideIcon } from "lucide-react";

import { Badge, TabsList, TabsRoot, TabsTrigger } from "./ui";

export type TabItem<K extends string = string> = {
  key: K;
  label: string;
  icon?: LucideIcon;
  /** Small count shown as a pill on the tab (e.g. unread activity). */
  badge?: number | null;
};

/**
 * In-page sub-navigation: a segmented control that keeps a single page's
 * large sections behind tabs instead of one long scroll. Pages own the active
 * key so the choice can be lifted (e.g. a deep-link from another surface).
 *
 * Backed by Radix Tabs so arrow keys, focus management, and tab semantics stay
 * native. Panels are rendered by the caller and should reference `${idBase}-panel`.
 */
export function Tabs<K extends string>({
  tabs,
  active,
  onChange,
  idBase,
  ariaLabel,
}: {
  tabs: TabItem<K>[];
  active: K;
  onChange: (key: K) => void;
  idBase: string;
  ariaLabel: string;
}) {
  return (
    <TabsRoot value={active} onValueChange={(value) => onChange(value as K)}>
      <TabsList
        className="grid w-full sm:w-fit"
        style={{ gridTemplateColumns: `repeat(${tabs.length}, minmax(0, 1fr))` }}
        aria-label={ariaLabel}
      >
        {tabs.map((tab) => {
          const Icon = tab.icon;
          return (
            <TabsTrigger
              key={tab.key}
              id={`${idBase}-tab-${tab.key}`}
              className="min-w-0 gap-1 px-2 sm:gap-1.5 sm:px-3 [&>span]:min-w-0 [&>span]:truncate"
              value={tab.key}
              aria-controls={`${idBase}-panel`}
            >
              {Icon ? <Icon size={15} aria-hidden="true" /> : null}
              <span>{tab.label}</span>
              {tab.badge ? (
                <Badge
                  variant="secondary"
                  className="h-4 min-w-4 px-1 text-[10px]"
                  aria-label={`${tab.badge} new`}
                >
                  {tab.badge > 9 ? "9+" : tab.badge}
                </Badge>
              ) : null}
            </TabsTrigger>
          );
        })}
      </TabsList>
    </TabsRoot>
  );
}
