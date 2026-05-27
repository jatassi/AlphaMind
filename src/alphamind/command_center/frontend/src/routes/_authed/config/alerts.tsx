import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { AlertsConfigPage } from '@/views/configuration/alerts-page'

// /config/alerts — alerts rule registry editor (story 06b / ALP-683).
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/config/alerts',
  component: AlertsConfigPage,
})
