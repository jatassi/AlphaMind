// /config/regimes — regime family landing page (Wave-6 #13).
// Inherits the protected AuthedLayout (Nav + AlertBanner) from _authed.tsx.
import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { RegimeIndexPage } from '@/views/configuration/regimes/regime-index-page'

export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/config/regimes',
  component: RegimeIndexPage,
})
