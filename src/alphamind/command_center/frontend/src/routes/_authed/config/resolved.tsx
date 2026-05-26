import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { ResolvedConfigPage } from '@/views/configuration/resolved/resolved-config-page'

// Resolved-config viewer — /config/resolved (ALP-684 story 06c).
// Inherits the protected AuthedLayout from _authed.tsx.
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/config/resolved',
  component: ResolvedConfigPage,
})
