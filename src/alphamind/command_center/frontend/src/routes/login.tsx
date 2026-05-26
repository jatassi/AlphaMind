import { createRoute } from '@tanstack/react-router'

import { Route as RootRoute } from './__root'
import { LoginPage } from './login-page'

// Login route — public, no session required. The page component lives in
// `./login-page` so this file exports only the `Route` constant.
export const Route = createRoute({
  getParentRoute: () => RootRoute,
  path: '/login',
  component: LoginPage,
})
