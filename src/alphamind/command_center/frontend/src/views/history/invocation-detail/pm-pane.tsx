// PM envelopes pane for the per-invocation detail page (ALP-674).
//
// Renders PM_DECISION, ENVELOPE_REJECTED, and ENVELOPE_PARSE_FAILED
// activity-log entries. Rejected proposals render with equal prominence to
// approved ones per the design doc.

import type { InvocationActivityEntry } from '@/api/queries'

import { NarrativeWithChips } from './ref-id-chip'
import type { RefPrefix } from './ref-id-utils'

type ParsedPmDecision = {
  verdict?: string
  commands?: unknown[]
  modifications?: string
  concerns?: string
  rationale?: string
  anti_patterns?: string[]
}

function parsePmDetail(detail_json: string): ParsedPmDecision {
  try {
    return JSON.parse(detail_json) as ParsedPmDecision
  } catch {
    return {}
  }
}

const VERDICT_CLS: Record<string, string> = {
  APPROVE: 'bg-green-100 text-green-800',
  REJECT: 'bg-red-100 text-red-800',
  MODIFY: 'bg-yellow-100 text-yellow-800',
}

type VerdictPillProps = { verdict: string | undefined }

function VerdictPill({ verdict }: VerdictPillProps): React.JSX.Element {
  const label = verdict ?? 'UNKNOWN'
  const cls = VERDICT_CLS[label] ?? 'bg-muted text-muted-foreground'
  return <span className={`rounded px-2 py-0.5 text-xs font-semibold ${cls}`}>{label}</span>
}

type PmEnvelopeCardProps = {
  entry: InvocationActivityEntry
  onChipClick: (refId: string, prefix: RefPrefix) => void
}

function PmEnvelopeCard({ entry, onChipClick }: PmEnvelopeCardProps): React.JSX.Element {
  const detail = parsePmDetail(entry.detail_json)
  return (
    <div className="space-y-2 rounded border p-4">
      <div className="flex items-center gap-2">
        <VerdictPill verdict={detail.verdict} />
        <span className="text-muted-foreground font-mono text-xs">{entry.event_type}</span>
        <span className="text-muted-foreground ml-auto text-xs">{entry.entry_at}</span>
      </div>
      {detail.rationale ? (
        <PmNarrative label="Rationale" text={detail.rationale} onChipClick={onChipClick} />
      ) : null}
      {detail.anti_patterns && detail.anti_patterns.length > 0 ? (
        <AntiPatternTags tags={detail.anti_patterns} />
      ) : null}
      {detail.concerns ? <PmTextField label="Concerns" text={detail.concerns} /> : null}
      {detail.modifications ? (
        <PmTextField label="Modifications" text={detail.modifications} />
      ) : null}
      {detail.commands && detail.commands.length > 0 ? (
        <div>
          <span className="text-xs font-medium">Commands ({detail.commands.length})</span>
        </div>
      ) : null}
    </div>
  )
}

type NarrativeProps = {
  label: string
  text: string
  onChipClick: (refId: string, prefix: RefPrefix) => void
}

function PmNarrative({ label, text, onChipClick }: NarrativeProps): React.JSX.Element {
  return (
    <div>
      <span className="text-xs font-medium">{label}</span>
      <p className="mt-1 text-sm leading-relaxed">
        <NarrativeWithChips text={text} onChipClick={onChipClick} />
      </p>
    </div>
  )
}

function PmTextField({ label, text }: { label: string; text: string }): React.JSX.Element {
  return (
    <div>
      <span className="text-xs font-medium">{label}</span>
      <p className="mt-1 text-sm">{text}</p>
    </div>
  )
}

function AntiPatternTags({ tags }: { tags: string[] }): React.JSX.Element {
  return (
    <div>
      <span className="text-xs font-medium">Anti-patterns</span>
      <div className="mt-1 flex flex-wrap gap-1">
        {tags.map((ap) => (
          <span key={ap} className="rounded bg-red-50 px-1.5 py-0.5 text-xs text-red-700">
            {ap}
          </span>
        ))}
      </div>
    </div>
  )
}

type PmPaneProps = {
  entries: InvocationActivityEntry[]
  onChipClick: (refId: string, prefix: RefPrefix) => void
}

export function PmPane({ entries, onChipClick }: PmPaneProps): React.JSX.Element {
  return (
    <section aria-label="PM envelopes">
      <h2 className="mb-2 text-lg font-semibold">PM envelopes</h2>
      {entries.length === 0 ? (
        <p className="text-muted-foreground text-sm">No PM decision entries for this invocation.</p>
      ) : (
        <div className="space-y-3">
          {entries.map((e) => (
            <PmEnvelopeCard key={e.entry_id} entry={e} onChipClick={onChipClick} />
          ))}
        </div>
      )}
    </section>
  )
}
