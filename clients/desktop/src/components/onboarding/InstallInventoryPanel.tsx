import {
  AlertCircle,
  CheckCircle2,
  CircleDashed,
} from "lucide-react";

import type { SetupInstallInventory, SetupInstallItem, SetupStatus } from "../../types";
import { Badge, Card, CardContent } from "../ui";
import { cn } from "@/lib/utils";

const PRIMARY_ITEMS = [
  "home",
  "env",
  "agents",
  "repos",
  "repo-map",
  "cast",
  "slack",
  "memory",
  "token",
];
const REPO_MAP_PREVIEW_LIMIT = 4;

export function InstallInventoryPanel({
  inventory,
  queue,
  compact = false,
}: {
  inventory: SetupInstallInventory | null | undefined;
  queue?: SetupStatus["queue"] | null;
  compact?: boolean;
}) {
  if (!inventory?.initialized) {
    return null;
  }

  const queueMissingCount = queue?.missing_selected?.length ?? 0;
  const queueItem =
    queue && (!queue.ready || queue.covers_selected === false || queueMissingCount > 0)
      ? queueInventoryItem(queue)
      : null;
  const items = PRIMARY_ITEMS
    .map((key) => inventory.items.find((item) => item.key === key))
    .filter((item): item is SetupInstallItem => Boolean(item))
    .concat(queueItem ? [queueItem] : []);
  const blocking = items.filter((item) => !item.ok && !item.optional).length;

  return (
    <Card className="rounded-lg border-border/70 bg-background/60 text-left shadow-none">
      <CardContent className={cn("grid gap-3 px-3", compact ? "py-3" : "py-4")}>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <strong className="block text-sm font-medium text-foreground">
              Found an Alfred setup on this Mac
            </strong>
            <span className="block break-all text-xs text-muted-foreground">
              {inventory.alfred_home}
            </span>
          </div>
          <Badge variant={blocking ? "outline" : "secondary"}>
            {blocking ? `${blocking} to finish` : "ready to use"}
          </Badge>
        </div>

        <ul className="grid gap-2" aria-label="Detected Alfred setup">
          {items.map((item) => (
            <li
              key={item.key}
              className={cn(
                "grid grid-cols-[auto_1fr] gap-2 rounded-md border px-2.5 py-2 text-sm",
                item.ok
                  ? "border-primary/20 bg-primary/5"
                  : item.optional
                    ? "border-border/60 bg-muted/25"
                    : "border-destructive/25 bg-destructive/10",
              )}
            >
              <StatusIcon item={item} />
              <span className="min-w-0">
                <span className="flex flex-wrap items-center gap-1.5">
                  <strong className="font-medium text-foreground">{item.label}</strong>
                  {item.optional ? (
                    <Badge variant="outline" className="font-normal">
                      optional
                    </Badge>
                  ) : null}
                </span>
                <span className="block text-xs text-muted-foreground">{item.detail}</span>
                {item.path ? (
                  <code className="mt-1 block break-all text-[11px] text-muted-foreground">
                    {item.path}
                  </code>
                ) : null}
                {item.key === "repo-map" && inventory.repo_local_map?.entries.length ? (
                  <RepoMapPreview inventory={inventory} />
                ) : null}
              </span>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function RepoMapPreview({ inventory }: { inventory: SetupInstallInventory }) {
  const entries = inventory.repo_local_map?.entries ?? [];
  const visible = entries.slice(0, REPO_MAP_PREVIEW_LIMIT);
  const hidden = entries.length - visible.length;

  return (
    <span className="mt-2 grid gap-1" aria-label="Configured repo local path map">
      {visible.map((entry) => (
        <span
          key={entry.repo}
          className="grid gap-0.5 rounded border border-border/50 bg-background/55 px-2 py-1"
        >
          <code className="break-all text-[11px] text-foreground">{entry.repo}</code>
          <code className="break-all text-[11px] text-muted-foreground">{entry.path}</code>
        </span>
      ))}
      {hidden > 0 ? (
        <span className="text-[11px] text-muted-foreground">
          +{hidden} more {hidden === 1 ? "mapping" : "mappings"}
        </span>
      ) : null}
    </span>
  );
}

function queueInventoryItem(queue: NonNullable<SetupStatus["queue"]>): SetupInstallItem {
  const missing = queue.missing_selected ?? [];
  const detail = missing.length
    ? `Queue actions are missing ${missing.length} selected repo${missing.length === 1 ? "" : "s"}: ${missing.join(", ")}.`
    : "Queue actions are not ready for the selected repositories.";

  return {
    key: "queue",
    label: "Queue coverage",
    ok: false,
    detail,
    path: null,
  };
}

function StatusIcon({ item }: { item: SetupInstallItem }) {
  if (item.ok) {
    return <CheckCircle2 className="mt-0.5 text-primary" size={15} aria-hidden="true" />;
  }
  if (item.optional) {
    return <CircleDashed className="mt-0.5 text-muted-foreground" size={15} aria-hidden="true" />;
  }
  return <AlertCircle className="mt-0.5 text-destructive" size={15} aria-hidden="true" />;
}
