import { useNavigate } from '@tanstack/react-router'

import { useRunHistoryFailureLog } from '@/api/queries'
import { RunList } from '@/views/history/run-list'
import type { RunHistoryFilters } from '@/views/history/types'

import { Route } from './failures'

// Failure log page — /history/failures.
// Reuses RunList with isPreset=true so the status filter controls are hidden.
export function FailuresPage(): React.JSX.Element {
  const navigate = useNavigate({ from: Route.fullPath })
  const search = Route.useSearch()

  const filters: RunHistoryFilters = {
    date_from: search.date_from,
    date_to: search.date_to,
    run_type: search.run_type,
    page: search.page ?? 1,
    page_size: search.page_size ?? 50,
  }

  const { data, isLoading } = useRunHistoryFailureLog(filters)

  function handleFilterChange(next: RunHistoryFilters): void {
    void navigate({
      search: {
        date_from: next.date_from,
        date_to: next.date_to,
        run_type: next.run_type,
        page: next.page,
        page_size: next.page_size,
      },
    })
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Failure log</h1>
        {data !== undefined && (
          <span className="text-muted-foreground text-sm">
            {data.total} {data.total === 1 ? 'failure' : 'failures'}
          </span>
        )}
      </div>
      <RunList
        rows={data?.items ?? []}
        isLoading={isLoading}
        isPreset
        filters={filters}
        onFilterChange={handleFilterChange}
      />
    </div>
  )
}
