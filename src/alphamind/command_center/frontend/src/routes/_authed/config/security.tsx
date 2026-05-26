import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { SecurityConfigPage } from '@/views/configuration/security-page'

// /config/security — session + WebAuthn relying-party editor (story 06b / ALP-683).
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/config/security',
  component: SecurityConfigPage,
})
