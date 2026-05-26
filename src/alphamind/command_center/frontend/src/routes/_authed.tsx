import { createRoute } from '@tanstack/react-router'

import { Route as RootRoute } from './__root'
import { AuthedLayout } from './_authed-layout'

// Protected route node. The layout component lives in `./_authed-layout`
// so this file exports only the `Route` constant — view stories add their
// own routes here via `getParentRoute: () => Route`.
//
// `id: '_authed'` makes this a layout route — it has no path segment, so
// its children mount at the URL of their own `path` prop relative to the
// root (e.g. an `IndexRoute` with `path: '/'` mounts at `/`).
export const Route = createRoute({
  getParentRoute: () => RootRoute,
  id: '_authed',
  component: AuthedLayout,
})
