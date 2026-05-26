// CronExpressionPicker — structured "every N hours anchored at HH:MM"
// picker with the read-only cron preview the design calls for (ALP-679).
//
// The picker carries a narrow contract: the operator picks an N-hour
// cadence and an HH:MM anchor; the editor recomposes the cron expression
// from those two inputs. Free-form cron strings remain editable via the
// scheduler.yaml file directly when the operator wants something the
// picker doesn't express; this is the v1 surface per the design.

import { useMemo } from 'react'

import { cn } from '@/lib/utils'

type CronExpressionPickerProps = {
  path: string
  label: string
  value: string
  onChange: (next: string) => void
  disabled?: boolean
  className?: string
}

type ParsedCron = {
  everyNHours: number
  anchorMinute: number
}

function parseEveryNHours(value: string): ParsedCron {
  const parts = value.trim().split(/\s+/)
  const fallback: ParsedCron = { everyNHours: 4, anchorMinute: 0 }
  if (parts.length < 2) {
    return fallback
  }
  const anchorMinute = Number(parts[0])
  const hourParts = parts[1].split(',').map(Number)
  if (!Number.isFinite(anchorMinute) || hourParts.some((h) => !Number.isFinite(h))) {
    return fallback
  }
  if (hourParts.length < 2) {
    return { everyNHours: 24, anchorMinute }
  }
  const gap = hourParts[1] - hourParts[0]
  return { everyNHours: gap > 0 ? gap : 4, anchorMinute }
}

function composeCron(parsed: ParsedCron, weekdaySpec: string): string {
  const hours: number[] = []
  for (let h = 0; h < 24; h += parsed.everyNHours) {
    hours.push(h)
  }
  return `${parsed.anchorMinute.toString()} ${hours.join(',')} * * ${weekdaySpec}`
}

function readWeekdaySpec(value: string): string {
  const parts = value.trim().split(/\s+/)
  return parts.length >= 5 ? parts.slice(4).join(' ') : '*'
}

type CommitContext = {
  parsed: ParsedCron
  weekdaySpec: string
  onChange: (next: string) => void
}

function commitHours(rawText: string, ctx: CommitContext): void {
  const next = Number(rawText)
  if (Number.isFinite(next) && next >= 1 && next <= 24) {
    ctx.onChange(composeCron({ ...ctx.parsed, everyNHours: next }, ctx.weekdaySpec))
  }
}

function commitMinute(rawText: string, ctx: CommitContext): void {
  const next = Number(rawText)
  if (Number.isFinite(next) && next >= 0 && next <= 59) {
    ctx.onChange(composeCron({ ...ctx.parsed, anchorMinute: next }, ctx.weekdaySpec))
  }
}

function StructuredInputs({
  path,
  parsed,
  weekdaySpec,
  disabled,
  onChange,
}: {
  path: string
  parsed: ParsedCron
  weekdaySpec: string
  disabled: boolean | undefined
  onChange: (next: string) => void
}): React.JSX.Element {
  return (
    <div className="flex items-center gap-2">
      <label htmlFor={`${path}-every`} className="text-muted-foreground text-xs">
        Every N hours
      </label>
      <input
        id={`${path}-every`}
        type="number"
        min={1}
        max={24}
        defaultValue={parsed.everyNHours}
        disabled={disabled}
        onChange={(e) => {
          commitHours(e.target.value, { parsed, weekdaySpec, onChange })
        }}
        className="border-input bg-background h-9 w-20 rounded-md border px-2 py-1 text-sm disabled:cursor-not-allowed disabled:opacity-50"
      />
      <label htmlFor={`${path}-anchor`} className="text-muted-foreground text-xs">
        Anchor minute
      </label>
      <input
        id={`${path}-anchor`}
        type="number"
        min={0}
        max={59}
        defaultValue={parsed.anchorMinute}
        disabled={disabled}
        onChange={(e) => {
          commitMinute(e.target.value, { parsed, weekdaySpec, onChange })
        }}
        className="border-input bg-background h-9 w-20 rounded-md border px-2 py-1 text-sm disabled:cursor-not-allowed disabled:opacity-50"
      />
    </div>
  )
}

export function CronExpressionPicker({
  path,
  label,
  value,
  onChange,
  disabled,
  className,
}: CronExpressionPickerProps): React.JSX.Element {
  const parsed = useMemo(() => parseEveryNHours(value), [value])
  const weekdaySpec = useMemo(() => readWeekdaySpec(value), [value])
  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <span className="text-sm font-medium">{label}</span>
      <StructuredInputs
        // Re-mount the inputs when value changes externally so defaultValue
        // tracks the new parsed numbers; this also gives user.clear() the
        // expected uncontrolled-input behavior in the test layer.
        key={value}
        path={path}
        parsed={parsed}
        weekdaySpec={weekdaySpec}
        disabled={disabled}
        onChange={onChange}
      />
      <pre className="bg-muted text-muted-foreground rounded-md px-3 py-2 font-mono text-xs">
        {value}
      </pre>
    </div>
  )
}
