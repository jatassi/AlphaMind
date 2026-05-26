import { useEffect } from 'react'

import { Outlet, useNavigate } from '@tanstack/react-router'

import { useSession } from '@/api/queries'
import { AlertBanner } from '@/components/alert-banner'
import { Nav } from '@/components/nav'

// Protected layout component. Calls `useSession()` to gate access; if the
// session is null (401 from /auth/me) redirect to /login. While the query
// is in flight render a minimal loading shell so the user doesn't flash an
// authenticated view they're about to be kicked out of.
//
// Lives in its own file so the matching route definition file
// (`./_authed.tsx`) can re-export only the `Route` constant — the
// `react-refresh/only-export-components` rule fast-refreshes files that
// exclusively export components, and a mixed file (component + Route
// constant) doesn't qualify.

export function AuthedLayout(): React.JSX.Element | null {
  const navigate = useNavigate()
  const session = useSession()

  useEffect(() => {
    if (!session.isPending && session.data === null) {
      void navigate({ to: '/login' })
    }
  }, [session.isPending, session.data, navigate])

  if (session.isPending) {
    return <div className="flex min-h-screen items-center justify-center">Loading...</div>
  }
  if (session.data === null) {
    // Redirect in flight — render nothing so we don't flash the layout.
    return null
  }

  return (
    <div className="min-h-screen">
      <Nav />
      <AlertBanner />
      <main className="container mx-auto p-4">
        <Outlet />
      </main>
    </div>
  )
}
