import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { ConfigHistoryPage } from '@/views/configuration/history/history-page'

// Config git-history viewer — /config/history (ALP-684 story 06c).
// Inherits the protected AuthedLayout from _authed.tsx.
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/config/history',
  component: ConfigHistoryPage,
})
