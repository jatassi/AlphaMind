// Activity log explorer page — the main view composing filter controls,
// the table, pagination, and the saved-filter sidebar.
//
// Filter state is persisted in URL search params via TanStack Router's
// `useSearch` / `useNavigate`. Changes to filters reset `page` to 1 so the
// user doesn't land on a now-invalid page number.

import { useCallback } from 'react'

import { useNavigate, useSearch } from '@tanstack/react-router'

import { type SavedFilter, useActivityLog } from '@/api/queries'

import { ActivityLogTable } from './activity-log-table'
import { FilterHeader } from './filter-header'
import {
  type FilterState,
  filterStateToApiFilters,
  filterStateToSearch,
  searchToFilterState,
} from './filter-state'
import { PaginationControls } from './pagination-controls'
import { SavedFilterSidebar } from './saved-filter-sidebar'

function useFilterState(): [FilterState, (update: Partial<FilterState>) => void] {
  const navigate = useNavigate()
  // useSearch with strict:false returns the search object as-is without
  // requiring a specific route match.
  const rawSearch = useSearch({ strict: false })
  const filters = searchToFilterState(rawSearch)

  const update = useCallback(
    (patch: Partial<FilterState>) => {
      const next = { ...filters, ...patch }
      void navigate({
        to: '/activity-log',
        search: filterStateToSearch(next),
        replace: true,
      })
    },
    [filters, navigate],
  )

  return [filters, update]
}

function applySavedFilter(
  preset: SavedFilter,
  current: FilterState,
  update: (patch: Partial<FilterState>) => void,
): void {
  const patch: Partial<FilterState> = {
    page: 1,
    source: preset.params.source ?? current.source,
    event_type: preset.params.event_type ?? current.event_type,
  }
  update(patch)
}

export function ActivityLogPage(): React.JSX.Element {
  const [filters, updateFilters] = useFilterState()
  const apiFilters = filterStateToApiFilters(filters)
  const query = useActivityLog(apiFilters)

  const onApplySaved = useCallback(
    (preset: SavedFilter) => applySavedFilter(preset, filters, updateFilters),
    [filters, updateFilters],
  )

  const onPageChange = useCallback((page: number) => updateFilters({ page }), [updateFilters])

  const pageData = query.data ?? {
    rows: [],
    total: 0,
    page: filters.page,
    page_size: filters.page_size,
    has_more: false,
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Activity log</h1>

      <FilterHeader filters={filters} onChange={updateFilters} />

      <div className="flex gap-4">
        <div className="min-w-0 flex-1 space-y-3">
          <ActivityLogTable rows={pageData.rows} isLoading={query.isPending} />
          <PaginationControls
            page={pageData.page}
            pageSize={pageData.page_size}
            total={pageData.total}
            hasMore={pageData.has_more}
            onPageChange={onPageChange}
          />
        </div>

        <SavedFilterSidebar onApply={onApplySaved} />
      </div>
    </div>
  )
}

export { DEFAULT_FILTER_STATE } from './filter-state'
