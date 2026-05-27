// Position detail stub — placeholder component for story 05g (ALP-676).
// Rendered at /portfolio/positions/$positionId until the full detail view
// lands in story 05g.

import { useParams } from '@tanstack/react-router'

export function PositionDetailStub(): React.JSX.Element {
  const params = useParams({ strict: false })
  const positionId = params.positionId ?? '(unknown)'
  return (
    <div className="p-4">
      <h1 className="text-2xl font-semibold">Position detail</h1>
      <p className="text-muted-foreground mt-2 text-sm">
        Position <code>{positionId}</code> — full detail view ships in story 05g.
      </p>
    </div>
  )
}
