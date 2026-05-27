import { createRouter } from '@tanstack/react-router'

import { Route as RootRoute } from './routes/__root'
import { Route as AuthedRoute } from './routes/_authed'
import { Route as ConfigAlertsRoute } from './routes/_authed/config/alerts'
import { Route as ConfigCommandCenterRoute } from './routes/_authed/config/command-center'
import { Route as ConfigDigestRoute } from './routes/_authed/config/digest'
import { Route as ConfigHistoryRoute } from './routes/_authed/config/history'
import { Route as ProfileNameRoute } from './routes/_authed/config/profiles/$profile-name'
import { Route as ProfileIndexRoute } from './routes/_authed/config/profiles/index'
import { Route as RegimeNameRoute } from './routes/_authed/config/regimes/$regime-name'
import { Route as RegimeIndexRoute } from './routes/_authed/config/regimes/index'
import { Route as ConfigResolvedRoute } from './routes/_authed/config/resolved'
import { Route as ConfigSecurityRoute } from './routes/_authed/config/security'
import { Route as InvocationDetailRoute } from './routes/_authed/history/$invocation-id'
import { Route as BriefViewerRoute } from './routes/_authed/history/brief-viewer'
import { Route as FailuresRoute } from './routes/_authed/history/failures'
import { Route as HistoryRoute } from './routes/_authed/history/index'
import { Route as LiveRoute } from './routes/_authed/live'
import { Route as PortfolioRoute } from './routes/_authed/portfolio/index'
import { Route as PositionDetailRoute } from './routes/_authed/portfolio/positions/$position-id'
import { Route as ThesisDetailRoute } from './routes/_authed/portfolio/theses/$thesis-id'
import { Route as ThesesRoute } from './routes/_authed/portfolio/theses/index'
import { Route as CalibrationMixRoute } from './routes/_authed/risk/calibration-mix'
import { Route as RiskRoute } from './routes/_authed/risk/index'
import { Route as RegimeTimelineRoute } from './routes/_authed/risk/timeline'
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
    ThesesRoute,
    ThesisDetailRoute,
    RiskRoute,
    RegimeTimelineRoute,
    CalibrationMixRoute,
    // 06a: profiles + regimes config editor pages.
    // Index landing pages registered BEFORE the $name routes so the
    // exact ``/config/profiles`` and ``/config/regimes`` paths bind to
    // the FilePicker landing rather than to a ``$profileName="profiles"``
    // edit page (TanStack Router matches by registration order when
    // both routes share a prefix).
    ProfileIndexRoute,
    ProfileNameRoute,
    RegimeIndexRoute,
    RegimeNameRoute,
    // 06b: per-file config editor routes.
    ConfigAlertsRoute,
    ConfigSecurityRoute,
    ConfigCommandCenterRoute,
    ConfigDigestRoute,
    // 06c: config diagnostic views (ALP-684).
    ConfigResolvedRoute,
    ConfigHistoryRoute,
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
