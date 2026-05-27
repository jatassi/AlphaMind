import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '../../_authed'
import { FailuresPage } from './failures-page'

// /history/failures route — preset view for failed and partial runs.
// Uses the /api/views/history/runs/preset/failure-log endpoint.
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/history/failures',
  component: FailuresPage,
  validateSearch: (search: Record<string, unknown>) => {
    return {
      date_from: typeof search.date_from === 'string' ? search.date_from : undefined,
      date_to: typeof search.date_to === 'string' ? search.date_to : undefined,
      run_type: typeof search.run_type === 'string' ? search.run_type : undefined,
      page: typeof search.page === 'number' ? search.page : undefined,
      page_size: typeof search.page_size === 'number' ? search.page_size : undefined,
    }
  },
})
