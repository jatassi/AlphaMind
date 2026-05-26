import { createRouter } from '@tanstack/react-router'

import { Route as RootRoute } from './routes/__root'
import { Route as AuthedRoute } from './routes/_authed'
import { Route as InvocationDetailRoute } from './routes/_authed/history/$invocation-id'
import { Route as BriefViewerRoute } from './routes/_authed/history/brief-viewer'
import { Route as FailuresRoute } from './routes/_authed/history/failures'
import { Route as HistoryRoute } from './routes/_authed/history/index'
import { Route as LiveRoute } from './routes/_authed/live'
import { Route as PortfolioRoute } from './routes/_authed/portfolio/index'
import { Route as PositionDetailRoute } from './routes/_authed/portfolio/positions/$position-id'
import { Route as ActivityLogRoute } from './routes/activity-log'
import { Route as IndexRoute } from './routes/index'
import { Route as LoginRoute } from './routes/login'

// Compose the TanStack Router tree. View stories add children to AuthedRoute
// via `addChildren` here.
const routeTree = RootRoute.addChildren([
  LoginRoute,
  AuthedRoute.addChildren([
    IndexRoute,
    LiveRoute,
    HistoryRoute,
    // 05d: brief-viewer registered before the $invocationId catch-all so the
    // static path /history/brief-viewer is matched first.
    BriefViewerRoute,
    FailuresRoute,
    InvocationDetailRoute,
    ActivityLogRoute,
    PortfolioRoute,
    PositionDetailRoute,
  ]),
])

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
