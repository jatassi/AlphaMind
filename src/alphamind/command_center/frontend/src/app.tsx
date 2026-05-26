import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'

import { router } from './router'

// Outermost app layout. Wraps the router with the QueryClient provider so
// every view's TanStack Query hooks share one cache. The QueryClient is
// constructed once at module scope — re-creating it on each render would
// throw away the cache + active queries.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

export function App(): React.JSX.Element {
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  )
}
