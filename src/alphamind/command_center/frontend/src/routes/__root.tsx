import { createRootRoute, Outlet } from '@tanstack/react-router'

// Root layout — the outermost route in the TanStack Router tree. The login
// route lives at the root level (no nav shell); the protected views live
// under `_authed/` which mounts the Nav + AlertBanner before its <Outlet />.
//
// View stories (05b–05j, 06a–06c) add their routes as children of
// `_authed/` so they automatically inherit the protected layout.
export const Route = createRootRoute({
  component: () => <Outlet />,
})
