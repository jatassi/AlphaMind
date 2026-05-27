import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { PortfolioDashboardPage } from '@/views/portfolio/dashboard/portfolio-dashboard-page'

// Portfolio dashboard route — /portfolio (ALP-676).
// Inherits the protected AuthedLayout (Nav + AlertBanner) from _authed.tsx.
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/portfolio',
  component: PortfolioDashboardPage,
})
