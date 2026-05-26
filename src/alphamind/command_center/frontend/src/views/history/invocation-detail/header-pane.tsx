// Header pane for the per-invocation detail page (ALP-674).
//
// Renders run type, timestamps, status, phase durations, git SHA, profile,
// regime, mode, abort reason and error summary from the InvocationDetailHeader.

import type { InvocationDetailHeader } from '@/api/queries'

const STATUS_CLS: Record<string, string> = {
  completed: 'bg-green-100 text-green-800',
  failed: 'bg-red-100 text-red-800',
  partial: 'bg-yellow-100 text-yellow-800',
}

function StatusBadge({ status }: { status: string }): React.JSX.Element {
  const cls = STATUS_CLS[status] ?? 'bg-muted text-muted-foreground'
  return <span className={`rounded px-2 py-0.5 text-xs font-semibold ${cls}`}>{status}</span>
}

function DurationCell({ secs }: { secs: number | null }): React.JSX.Element {
  if (secs === null) {
    return <span className="text-muted-foreground">—</span>
  }
  const mins = Math.floor(secs / 60)
  const rem = Math.round(secs % 60)
  return <span>{mins > 0 ? `${String(mins)}m ${String(rem)}s` : `${String(rem)}s`}</span>
}

type FieldRowProps = { label: string; value: React.ReactNode }

function FieldRow({ label, value }: FieldRowProps): React.JSX.Element {
  return (
    <div className="grid grid-cols-[180px_1fr] gap-2 border-b py-1 text-sm last:border-none">
      <span className="text-muted-foreground font-medium">{label}</span>
      <span className="font-mono">{value}</span>
    </div>
  )
}

type HeaderPaneProps = { header: InvocationDetailHeader }

export function HeaderPane({ header }: HeaderPaneProps): React.JSX.Element {
  const hasAbortReason = header.abort_reason !== null
  const hasErrorSummary = header.error_summary !== null && header.error_summary !== 'none'
  return (
    <section aria-label="Invocation header">
      <h2 className="mb-2 text-lg font-semibold">Header</h2>
      <div className="rounded border p-3">
        <FieldRow label="Invocation ID" value={header.invocation_id} />
        <FieldRow label="Status" value={<StatusBadge status={header.status} />} />
        <FieldRow label="Run type" value={header.run_type} />
        <FieldRow label="Started at" value={header.started_at.replace('T', ' ')} />
        <FieldRow
          label="Ended at"
          value={header.ended_at ? header.ended_at.replace('T', ' ') : '—'}
        />
        <FieldRow label="Duration" value={<DurationCell secs={header.duration_seconds} />} />
        <FieldRow label="Phase 1 completed" value={header.phase1_completed_at ?? '—'} />
        <FieldRow label="Phase 2 completed" value={header.phase2_completed_at ?? '—'} />
        <FieldRow label="Trigger type" value={header.trigger_type} />
        <FieldRow label="Trigger reason" value={header.trigger_reason} />
        <FieldRow
          label="Git SHA"
          value={<span className="font-mono text-xs">{header.git_sha}</span>}
        />
        <FieldRow label="Profile" value={header.active_profile} />
        <FieldRow label="Regime" value={header.active_regime} />
        <FieldRow label="Mode" value={header.active_mode} />
        {hasAbortReason ? (
          <FieldRow
            label="Abort reason"
            value={<span className="text-red-600">{header.abort_reason}</span>}
          />
        ) : null}
        {hasErrorSummary ? (
          <FieldRow
            label="Error summary"
            value={<span className="text-red-600">{header.error_summary}</span>}
          />
        ) : null}
      </div>
    </section>
  )
}
