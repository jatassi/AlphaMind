// Thesis detail page — /portfolio/theses/$thesisId (ALP-678).
//
// Renders: summary, all components with type + narrative + key assumptions,
// status-history vertical timeline with cited reference-ID chips, and
// resolution outcome if resolved.

import { Link } from '@tanstack/react-router'

import type { ThesisComponentDetail, ThesisDetail } from '@/api/portfolio'
import { useThesisDetail } from '@/api/portfolio'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

import { StatusTimeline } from './status-timeline'

type ComponentCardProps = {
  comp: ThesisComponentDetail
  resolutionOutcome: string | undefined
}

function KeyAssumptionList({ assumptions }: { assumptions: string[] }): React.JSX.Element | null {
  if (assumptions.length === 0) {
    return null
  }
  return (
    <div>
      <p className="text-muted-foreground mb-1 text-xs font-medium">Key assumptions</p>
      <ul className="space-y-0.5">
        {assumptions.map((a) => (
          <li key={a} className="text-muted-foreground text-xs">
            • {a}
          </li>
        ))}
      </ul>
    </div>
  )
}

function ComponentCard({ comp, resolutionOutcome }: ComponentCardProps): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm font-medium">
          {comp.component_type.replaceAll('_', ' ')}
          {comp.instrument_reference ? (
            <span className="text-muted-foreground ml-2 font-normal">
              {comp.instrument_reference}
            </span>
          ) : null}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className="text-sm">{comp.narrative}</p>
        <KeyAssumptionList assumptions={comp.key_assumptions} />
        {resolutionOutcome ? (
          <p className="text-xs">
            <span className="text-muted-foreground">Resolution: </span>
            <span className="font-medium">{resolutionOutcome.replaceAll('_', ' ')}</span>
          </p>
        ) : null}
        {comp.resolution_notes ? (
          <p className="text-muted-foreground text-xs">{comp.resolution_notes}</p>
        ) : null}
      </CardContent>
    </Card>
  )
}

function ResolutionOutcomeSection({ thesis }: { thesis: ThesisDetail }): React.JSX.Element | null {
  if (thesis.status !== 'RESOLVED' || !thesis.resolution_category) {
    return null
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Resolution outcome</CardTitle>
      </CardHeader>
      <CardContent className="space-y-1">
        <p className="text-sm">
          <span className="text-muted-foreground">Category: </span>
          <span className="font-medium">{thesis.resolution_category.replaceAll('_', ' ')}</span>
        </p>
        {thesis.resolution_timestamp ? (
          <p className="text-muted-foreground text-xs">
            Resolved {new Date(thesis.resolution_timestamp).toLocaleString()}
          </p>
        ) : null}
      </CardContent>
    </Card>
  )
}

function ThesisHeader({ thesis }: { thesis: ThesisDetail }): React.JSX.Element {
  return (
    <div className="space-y-1">
      <h1 className="text-2xl font-semibold">{thesis.summary}</h1>
      <div className="text-muted-foreground flex gap-3 text-sm">
        <span>
          Status: <span className="text-foreground font-medium">{thesis.status}</span>
        </span>
        {thesis.thesis_status ? (
          <span>
            Classification:{' '}
            <span className="text-foreground font-medium">
              {thesis.thesis_status.replaceAll('_', ' ')}
            </span>
          </span>
        ) : null}
        <span>
          Position:{' '}
          <Link
            to="/portfolio/positions/$positionId"
            params={{ positionId: thesis.position_id }}
            className="text-primary font-mono underline-offset-2 hover:underline"
          >
            {thesis.position_id}
          </Link>
        </span>
      </div>
    </div>
  )
}

type DetailBodyProps = { thesis: ThesisDetail }

function ThesisDetailBody({ thesis }: DetailBodyProps): React.JSX.Element {
  return (
    <div className="space-y-6">
      <ThesisHeader thesis={thesis} />
      <ResolutionOutcomeSection thesis={thesis} />
      <section>
        <h2 className="mb-3 text-lg font-semibold">Components</h2>
        <div className="space-y-3">
          {thesis.components.map((comp) => (
            <ComponentCard
              key={comp.component_id}
              comp={comp}
              resolutionOutcome={thesis.resolution_component_outcomes[comp.component_id]}
            />
          ))}
        </div>
      </section>
      <section>
        <h2 className="mb-3 text-lg font-semibold">Status history</h2>
        <StatusTimeline history={thesis.status_history} />
      </section>
    </div>
  )
}

type Props = { thesisId: string }

export function ThesisDetailPage({ thesisId }: Props): React.JSX.Element {
  const { data, isPending, isError } = useThesisDetail(thesisId)

  if (isPending) {
    return (
      <div className="flex items-center justify-center p-8">
        <span className="text-muted-foreground">Loading thesis…</span>
      </div>
    )
  }

  if (isError) {
    return (
      <div className="flex items-center justify-center p-8">
        <span className="text-destructive">Failed to load thesis detail.</span>
      </div>
    )
  }

  return <ThesisDetailBody thesis={data} />
}
