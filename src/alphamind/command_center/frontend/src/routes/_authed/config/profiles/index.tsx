// /config/profiles — profile family landing page (Wave-6 #13).
// Inherits the protected AuthedLayout (Nav + AlertBanner) from _authed.tsx.
import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { ProfileIndexPage } from '@/views/configuration/profiles/profile-index-page'

export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/config/profiles',
  component: ProfileIndexPage,
})
