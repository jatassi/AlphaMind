import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { ThesisDetailPage } from '@/views/portfolio/theses/thesis-detail-page'

// Thesis detail route — /portfolio/theses/$thesisId (ALP-678).
// Inherits the protected AuthedLayout (Nav + AlertBanner) from _authed.tsx.
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/portfolio/theses/$thesisId',
  component: function ThesisDetailRoute() {
    const { thesisId } = Route.useParams()
    return <ThesisDetailPage thesisId={thesisId} />
  },
})
