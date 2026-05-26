import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { RegimeTimelinePage } from '@/views/risk/regime-timeline-page'

// Regime/overlay timeline route — /risk/timeline (ALP-680).
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/risk/timeline',
  component: RegimeTimelinePage,
})
