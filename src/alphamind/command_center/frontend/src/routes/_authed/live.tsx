import { createRoute } from '@tanstack/react-router'

import { LivePage } from '@/views/live/live-page'

import { Route as AuthedRoute } from '../_authed'

// Route for /live — the live run watcher landing view (story 05b / ALP-672).
// Mounts under the _authed layout so it is session-gated.
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/live',
  component: LivePage,
})
