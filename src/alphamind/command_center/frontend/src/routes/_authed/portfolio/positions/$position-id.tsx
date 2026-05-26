import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { PositionDetailStub } from '@/views/portfolio/position-detail-stub'

// Position detail route stub — /portfolio/positions/$positionId (story 05g).
// Wired here so position-row deep-links in the portfolio dashboard (05f) have
// a typed target.  The full position-detail view lands in 05g.
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/portfolio/positions/$positionId',
  component: PositionDetailStub,
})
