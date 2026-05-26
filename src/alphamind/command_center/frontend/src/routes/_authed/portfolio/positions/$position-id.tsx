import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { PositionDetailPage } from '@/views/portfolio/position-detail/position-detail-page'

// Position detail route — /portfolio/positions/$positionId (story 05g / ALP-677).
// Full three-tab detail view: state, thesis, history; Force Close button in header.
// PositionDetailPage reads positionId from useParams({ strict: false }) internally.
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/portfolio/positions/$positionId',
  component: PositionDetailPage,
})
