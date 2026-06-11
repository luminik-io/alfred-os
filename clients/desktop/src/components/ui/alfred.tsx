import * as React from "react"

import { cn } from "@/lib/utils"

export type AlfredTone = "ok" | "warn" | "error" | "idle" | "info" | "unknown"

function normalizeTone(tone: AlfredTone): Exclude<AlfredTone, "unknown"> {
  return tone === "unknown" ? "idle" : tone
}

function AlfredMetric({
  asDescription = false,
  className,
  detail,
  label,
  title,
  tone = "idle",
  value,
  ...props
}: React.ComponentProps<"div"> & {
  asDescription?: boolean
  detail?: React.ReactNode
  label: React.ReactNode
  title?: string
  tone?: AlfredTone
  value: React.ReactNode
}) {
  const Label = asDescription ? "dt" : "span"
  const Value = asDescription ? "dd" : "strong"
  return (
    <div
      data-slot="alfred-metric"
      data-tone={normalizeTone(tone)}
      className={cn("alfred-metric", className)}
      {...props}
    >
      <Label className="alfred-metric__label">{label}</Label>
      <Value className="alfred-metric__value" title={title}>
        {value}
      </Value>
      {detail ? <p className="alfred-metric__detail">{detail}</p> : null}
    </div>
  )
}

function AlfredStatusDot({
  className,
  tone = "idle",
  ...props
}: React.ComponentProps<"span"> & { tone?: AlfredTone }) {
  return (
    <span
      data-slot="alfred-status-dot"
      data-tone={normalizeTone(tone)}
      className={cn("alfred-status-dot", className)}
      {...props}
    />
  )
}

export { AlfredMetric, AlfredStatusDot }
