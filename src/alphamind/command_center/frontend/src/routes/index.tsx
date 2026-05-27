import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from './_authed'
import { IndexPage } from './index-page'

// Index route — the default landing page under the protected layout.
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/',
  component: IndexPage,
})
