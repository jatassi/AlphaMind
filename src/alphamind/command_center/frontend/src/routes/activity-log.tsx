import { createRoute } from '@tanstack/react-router'

import { ActivityLogPage } from '@/views/activity-log/activity-log-page'

import { Route as AuthedRoute } from './_authed'

// Activity log explorer route — filterable view over the full activity_log
// table (story 05e / ALP-675). Filter state lives in URL search params so
// the browser back/forward buttons navigate filter history correctly.
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/activity-log',
  component: ActivityLogPage,
})
