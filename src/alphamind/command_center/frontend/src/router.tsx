import { createRouter } from '@tanstack/react-router'

import { Route as RootRoute } from './routes/__root'
import { Route as AuthedRoute } from './routes/_authed'
import { Route as IndexRoute } from './routes/index'
import { Route as LoginRoute } from './routes/login'

// Compose the TanStack Router tree. View stories add children to AuthedRoute
// via `addChildren` here.
const routeTree = RootRoute.addChildren([LoginRoute, AuthedRoute.addChildren([IndexRoute])])

export const router = createRouter({
  routeTree,
  defaultPreload: 'intent',
})

declare module '@tanstack/react-router' {
  // eslint-disable-next-line @typescript-eslint/consistent-type-definitions
  interface Register {
    router: typeof router
  }
}
