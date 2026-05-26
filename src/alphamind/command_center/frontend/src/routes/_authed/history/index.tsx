import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '../../_authed'
import { HistoryPage } from './index-page'

function _parseStatus(raw: unknown): string[] | undefined {
  if (Array.isArray(raw)) {
    return raw as string[]
  }
  if (typeof raw === 'string') {
    return [raw]
  }
  return undefined
}

// /history route — paginated run history table under the protected layout.
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/history',
  component: HistoryPage,
  validateSearch: (search: Record<string, unknown>) => {
    return {
      date_from: typeof search.date_from === 'string' ? search.date_from : undefined,
      date_to: typeof search.date_to === 'string' ? search.date_to : undefined,
      run_type: typeof search.run_type === 'string' ? search.run_type : undefined,
      status: _parseStatus(search.status),
      page: typeof search.page === 'number' ? search.page : undefined,
      page_size: typeof search.page_size === 'number' ? search.page_size : undefined,
    }
  },
})
