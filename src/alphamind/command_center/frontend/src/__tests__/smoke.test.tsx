import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from '@tanstack/react-router'
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { LoginForm } from '@/auth/login'
import { Nav } from '@/components/nav'

// Smoke test — confirms the foundation compiles + the high-level shell
// mounts. Per-view stories add their own deeper tests; this is the AC
// verification gate ("the smoke test passes").

function buildNavRouter() {
  const root = createRootRoute({ component: Nav })
  const indexRoute = createRoute({
    getParentRoute: () => root,
    path: '/',
    component: () => null,
  })
  return createRouter({
    routeTree: root.addChildren([indexRoute]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })
}

function expectNavText(): void {
  expect(screen.getByText('AlphaMind Command Center')).toBeInTheDocument()
}

function buildQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
}

describe('frontend smoke', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the Nav shell standalone', async () => {
    render(<RouterProvider router={buildNavRouter()} />)
    await waitFor(expectNavText)
  })

  it('mounts the login form against a QueryClient', () => {
    render(
      <QueryClientProvider client={buildQueryClient()}>
        <LoginForm />
      </QueryClientProvider>,
    )
    expect(screen.getByRole('button', { name: /sign in with passkey/i })).toBeInTheDocument()
  })

  // Regression guard for Wave-6 finding #13: the Risk and Configuration
  // dropdowns must surface every documented page in the menu. Pre-fix the
  // Configuration menu omitted Profiles + Regimes; there was no Risk menu
  // at all (only the bare ``/risk`` link landed users on the dashboard
  // and the timeline + calibration-mix pages were unreachable from nav).
  it('lists Risk + Configuration dropdowns with every page link', async () => {
    render(<RouterProvider router={buildNavRouter()} />)
    // Buttons toggle each dropdown.
    expect(await screen.findByRole('button', { name: 'Risk' })).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Configuration' })).toBeInTheDocument()
  })
})
