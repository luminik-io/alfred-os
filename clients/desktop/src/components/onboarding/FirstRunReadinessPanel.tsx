import {
  AlertCircle,
  CheckCircle2,
  CircleDashed,
  CircleDotDashed,
} from "lucide-react";

import type { SetupFirstRunCheck, SetupFirstRunReadiness } from "../../types";
import { Badge, Card, CardContent } from "../ui";
import { cn } from "@/lib/utils";

const MAX_RECOMMENDED_ROWS = 3;

export function FirstRunReadinessPanel({
  readiness,
  compact = false,
}: {
  readiness: SetupFirstRunReadiness | null | undefined;
  compact?: boolean;
}) {
  if (!readiness) {
    return null;
  }

  const required = readiness.checks.filter((check) => check.tier === "required");
  const blockers = required.filter((check) => !check.ready);
  const recommended = readiness.checks
    .filter((check) => check.tier === "recommended")
    .sort((a, b) => Number(a.ready) - Number(b.ready));
  const optional = readiness.checks.filter((check) => check.tier === "optional" && check.ready);
  const recommendedRows = recommended.slice(0, MAX_RECOMMENDED_ROWS);

  return (
    <Card className="rounded-lg border-border/70 bg-background/60 text-left shadow-none">
      <CardContent className={cn("grid gap-3 px-3", compact ? "py-3" : "py-4")}>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <strong className="block text-sm font-medium text-foreground">
              Ready for first real run
            </strong>
            <span className="block text-xs text-muted-foreground">{readiness.headline}</span>
          </div>
          <Badge variant={readiness.ready ? "secondary" : "outline"}>
            {readiness.ready ? "ready" : `${blockers.length} blocking`}
          </Badge>
        </div>

        <div className="grid gap-2" aria-label="First run readiness checks">
          {blockers.length ? (
            <ReadinessGroup title="Required action" checks={blockers} />
          ) : (
            <div className="rounded-md border border-primary/20 bg-primary/5 px-2.5 py-2 text-sm">
              <span className="flex items-start gap-2">
                <CheckCircle2 className="mt-0.5 text-primary" size={15} aria-hidden="true" />
                <span>
                  <strong className="block font-medium text-foreground">
                    Required setup is ready.
                  </strong>
                  <span className="block text-xs text-muted-foreground">
                    GitHub, engine CLI, repo scope, queue coverage, local checkout, scheduler, and
                    Desktop actions are in place.
                  </span>
                </span>
              </span>
            </div>
          )}

          {recommendedRows.length ? (
            <ReadinessGroup title="Recommended next" checks={recommendedRows} />
          ) : null}

          {optional.length ? <ReadinessGroup title="Enabled optional" checks={optional} /> : null}
        </div>

        <p className="text-xs text-muted-foreground">
          {readiness.summary.required_ready} of {readiness.summary.required_total} required ready;
          {" "}
          {readiness.summary.recommended_ready} of {readiness.summary.recommended_total} recommended
          ready.
        </p>
      </CardContent>
    </Card>
  );
}

function ReadinessGroup({ title, checks }: { title: string; checks: SetupFirstRunCheck[] }) {
  return (
    <div className="grid gap-1.5">
      <span className="text-[11px] font-medium uppercase text-muted-foreground">
        {title}
      </span>
      <ul className="grid gap-1.5">
        {checks.map((check) => (
          <li
            key={check.key}
            className={cn(
              "grid grid-cols-[auto_1fr] gap-2 rounded-md border px-2.5 py-2 text-sm",
              check.ready
                ? "border-primary/20 bg-primary/5"
                : check.required
                  ? "border-destructive/25 bg-destructive/10"
                  : "border-border/60 bg-muted/25",
            )}
          >
            <ReadinessIcon check={check} />
            <span className="min-w-0">
              <span className="flex flex-wrap items-center gap-1.5">
                <strong className="font-medium text-foreground">{check.title}</strong>
                <Badge variant={check.ready ? "secondary" : "outline"} className="font-normal">
                  {check.ready ? "ready" : check.tier.replace(/_/g, " ")}
                </Badge>
              </span>
              <span className="block text-xs text-muted-foreground">{check.detail}</span>
              {!check.ready && check.action ? (
                <span className="mt-1 block text-xs text-foreground">{check.action}</span>
              ) : null}
              {check.path ? (
                <code className="mt-1 block break-all text-[11px] text-muted-foreground">
                  {check.path}
                </code>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ReadinessIcon({ check }: { check: SetupFirstRunCheck }) {
  if (check.ready) {
    return <CheckCircle2 className="mt-0.5 text-primary" size={15} aria-hidden="true" />;
  }
  if (check.required) {
    return <AlertCircle className="mt-0.5 text-destructive" size={15} aria-hidden="true" />;
  }
  if (check.tier === "optional") {
    return <CircleDashed className="mt-0.5 text-muted-foreground" size={15} aria-hidden="true" />;
  }
  return <CircleDotDashed className="mt-0.5 text-muted-foreground" size={15} aria-hidden="true" />;
}
