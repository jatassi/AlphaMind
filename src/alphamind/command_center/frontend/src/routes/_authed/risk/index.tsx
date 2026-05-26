import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { GuardrailDashboardPage } from '@/views/risk/guardrail-dashboard-page'

// Guardrail dashboard route — /risk (ALP-680).
// Inherits the protected AuthedLayout (Nav + AlertBanner) from _authed.tsx.
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/risk',
  component: GuardrailDashboardPage,
})
