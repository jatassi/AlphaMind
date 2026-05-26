// Status timeline — vertical list of thesis status transitions (ALP-678).
// Each entry shows the transition arrow, timestamp, and cited reference-ID chips.

import type { ThesisStatusTransition } from '@/api/portfolio'

import { ReferenceIdChip } from './reference-id-chip'

type Props = {
  history: ThesisStatusTransition[]
}

function fmtTs(ts: string): string {
  try {
    return new Date(ts).toLocaleString()
  } catch {
    return ts
  }
}

function TransitionArrow({
  old: oldStatus,
  next,
}: {
  old: string
  next: string
}): React.JSX.Element {
  return (
    <span className="text-sm">
      <span className="text-muted-foreground">{oldStatus.replaceAll('_', ' ')}</span>
      <span className="mx-1">→</span>
      <span className="font-medium">{next.replaceAll('_', ' ')}</span>
    </span>
  )
}

export function StatusTimeline({ history }: Props): React.JSX.Element {
  if (history.length === 0) {
    return <p className="text-muted-foreground text-sm">No status transitions recorded.</p>
  }

  return (
    <ol className="space-y-3">
      {history.map((entry) => (
        <li key={entry.entry_id} className="flex gap-3">
          {/* Timeline dot + line */}
          <div className="flex flex-col items-center">
            <div className="bg-primary mt-1 h-2.5 w-2.5 shrink-0 rounded-full" />
            <div className="bg-border mt-1 w-px grow" />
          </div>

          <div className="mb-3 space-y-1">
            <TransitionArrow old={entry.old_status} next={entry.new_status} />
            <p className="text-muted-foreground text-xs">{fmtTs(entry.entry_at)}</p>
            {entry.cited_reference_ids.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {entry.cited_reference_ids.map((refId) => (
                  <ReferenceIdChip key={refId} refId={refId} />
                ))}
              </div>
            )}
          </div>
        </li>
      ))}
    </ol>
  )
}
