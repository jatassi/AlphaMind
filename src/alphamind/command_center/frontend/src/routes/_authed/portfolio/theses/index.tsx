import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { ThesesPage } from '@/views/portfolio/theses/theses-page'

// Theses dashboard route — /portfolio/theses (ALP-678).
// Inherits the protected AuthedLayout (Nav + AlertBanner) from _authed.tsx.
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/portfolio/theses',
  component: ThesesPage,
})
