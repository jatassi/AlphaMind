// Position detail — Thesis tab (ALP-677).
// Renders: thesis summary, one card per component, supporting signals.

import type { ThesisComponentDetail, ThesisDetail } from '@/api/portfolio'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

type Props = { thesis: ThesisDetail | null }

function KeyAssumptions({ items }: { items: string[] }): React.JSX.Element | null {
  if (items.length === 0) {
    return null
  }
  return (
    <div>
      <p className="text-muted-foreground mb-1 text-xs font-medium tracking-wide uppercase">
        Key assumptions
      </p>
      <ul className="space-y-1">
        {items.map((assumption) => (
          <li key={assumption} className="text-sm">
            {assumption}
          </li>
        ))}
      </ul>
    </div>
  )
}

function SupportingSignals({ items }: { items: string[] }): React.JSX.Element | null {
  if (items.length === 0) {
    return null
  }
  return (
    <div>
      <p className="text-muted-foreground mb-1 text-xs font-medium tracking-wide uppercase">
        Supporting signals
      </p>
      <div className="flex flex-wrap gap-1">
        {items.map((signal) => (
          <span key={signal} className="bg-muted rounded px-2 py-0.5 font-mono text-xs">
            {signal}
          </span>
        ))}
      </div>
    </div>
  )
}

function ComponentCard({ comp }: { comp: ThesisComponentDetail }): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{comp.component_type}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm">{comp.narrative}</p>
        <KeyAssumptions items={comp.key_assumptions} />
        <SupportingSignals items={comp.supporting_signals} />
        {comp.linked_bracket_leg !== null && (
          <p className="text-muted-foreground text-xs">
            Linked bracket leg: <code>{comp.linked_bracket_leg}</code>
          </p>
        )}
        {comp.resolution_outcome !== null && (
          <p className="text-xs">
            Resolution: <span className="font-medium">{comp.resolution_outcome}</span>
            {comp.resolution_notes !== null && ` — ${comp.resolution_notes}`}
          </p>
        )}
      </CardContent>
    </Card>
  )
}

function ThesisMeta({ thesis }: { thesis: ThesisDetail }): React.JSX.Element {
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
      <dt className="text-muted-foreground">Status</dt>
      <dd className="font-medium">{thesis.status}</dd>
      {thesis.resolution_category !== null && (
        <>
          <dt className="text-muted-foreground">Resolution</dt>
          <dd>{thesis.resolution_category}</dd>
        </>
      )}
      {thesis.time_expectation_hours !== null && (
        <>
          <dt className="text-muted-foreground">Time expectation</dt>
          <dd className="tabular-nums">{thesis.time_expectation_hours.toFixed(1)}h</dd>
        </>
      )}
      <dt className="text-muted-foreground">Generated</dt>
      <dd className="text-xs tabular-nums">
        {new Date(thesis.generation_timestamp).toLocaleString()}
      </dd>
      {thesis.resolution_timestamp !== null && (
        <>
          <dt className="text-muted-foreground">Resolved</dt>
          <dd className="text-xs tabular-nums">
            {new Date(thesis.resolution_timestamp).toLocaleString()}
          </dd>
        </>
      )}
    </dl>
  )
}

function ThesisSummaryCard({ thesis }: { thesis: ThesisDetail }): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Thesis summary</CardTitle>
      </CardHeader>
      <CardContent>
        <ThesisMeta thesis={thesis} />
        <p className="mt-3 text-sm">{thesis.summary}</p>
        {thesis.position_size_rationale !== null && (
          <div className="mt-3">
            <p className="text-muted-foreground mb-1 text-xs font-medium tracking-wide uppercase">
              Position size rationale
            </p>
            <p className="text-sm">{thesis.position_size_rationale}</p>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export function ThesisTab({ thesis }: Props): React.JSX.Element {
  if (thesis === null) {
    return (
      <div className="py-8 text-center">
        <p className="text-muted-foreground text-sm">No thesis linked to this position.</p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <ThesisSummaryCard thesis={thesis} />
      {thesis.components.length > 0 && (
        <div className="space-y-3">
          <h3 className="text-muted-foreground text-sm font-medium tracking-wide uppercase">
            Components
          </h3>
          {thesis.components.map((comp) => (
            <ComponentCard key={comp.component_id} comp={comp} />
          ))}
        </div>
      )}
    </div>
  )
}
