import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRoute } from '@tanstack/react-router'

import { BriefViewer } from '@/views/history/brief-viewer'

import { Route as AuthedRoute } from '../../_authed'

const _qc = new QueryClient({ defaultOptions: { queries: { retry: 1 } } })

// /history/brief-viewer?invocation_id=...&ref_prefix=... (story 05d / ALP-674).
// Standalone deep-link route for the source-brief retrieval store viewer.
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/history/brief-viewer',
  validateSearch: (search: Record<string, unknown>) => ({
    invocation_id: typeof search.invocation_id === 'string' ? search.invocation_id : '',
    ref_prefix: typeof search.ref_prefix === 'string' ? search.ref_prefix : '',
  }),
  component: function BriefViewerPage(): React.JSX.Element {
    const { invocation_id, ref_prefix } = Route.useSearch()

    if (!invocation_id || !ref_prefix) {
      return (
        <div className="text-muted-foreground p-6 text-sm">
          Missing <code>invocation_id</code> or <code>ref_prefix</code> query parameters.
        </div>
      )
    }

    return (
      <QueryClientProvider client={_qc}>
        <div className="max-w-2xl p-6">
          <h1 className="mb-4 text-xl font-semibold">
            Source Brief — <code>{ref_prefix}</code>
          </h1>
          <p className="text-muted-foreground mb-4 font-mono text-sm">
            Invocation: {invocation_id}
          </p>
          <BriefViewer invocationId={invocation_id} refPrefix={ref_prefix} />
        </div>
      </QueryClientProvider>
    )
  },
})
