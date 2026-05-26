import { useNavigate } from '@tanstack/react-router'

import { useRunHistory } from '@/api/queries'
import { RunList } from '@/views/history/run-list'
import type { RunHistoryFilters } from '@/views/history/types'

import { Route } from './index'

// Run history page — /history.
//
// Filter state lives in URL search params via TanStack Router's
// validateSearch so back/forward navigation preserves filters.

type PaginationProps = {
  page: number
  pageSize: number
  total: number
  onPageChange: (p: number) => void
}

export function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
}: PaginationProps): React.JSX.Element {
  const totalPages = Math.ceil(total / pageSize)
  return (
    <div className="flex items-center gap-2 text-sm">
      <button
        type="button"
        disabled={page <= 1}
        onClick={() => onPageChange(page - 1)}
        className="rounded border px-3 py-1 disabled:opacity-50"
      >
        Previous
      </button>
      <span>
        Page {page} of {totalPages}
      </span>
      <button
        type="button"
        disabled={page >= totalPages}
        onClick={() => onPageChange(page + 1)}
        className="rounded border px-3 py-1 disabled:opacity-50"
      >
        Next
      </button>
    </div>
  )
}

export function HistoryPage(): React.JSX.Element {
  const navigate = useNavigate({ from: Route.fullPath })
  const search = Route.useSearch()
  const filters: RunHistoryFilters = {
    date_from: search.date_from,
    date_to: search.date_to,
    run_type: search.run_type,
    status: search.status,
    page: search.page ?? 1,
    page_size: search.page_size ?? 50,
  }
  const { data, isLoading } = useRunHistory(filters)
  const pageSize = filters.page_size ?? 50
  const showPagination = data !== undefined && data.total > pageSize

  function onFilter(next: RunHistoryFilters): void {
    void navigate({
      search: {
        date_from: next.date_from,
        date_to: next.date_to,
        run_type: next.run_type,
        status: next.status,
        page: next.page,
        page_size: next.page_size,
      },
    })
  }

  return (
    <div className="space-y-4">
      <PageHeader total={data?.total} />
      <RunList
        rows={data?.items ?? []}
        isLoading={isLoading}
        filters={filters}
        onFilterChange={onFilter}
      />
      {showPagination ? (
        <Pagination
          page={filters.page ?? 1}
          pageSize={pageSize}
          total={data.total}
          onPageChange={(p) => onFilter({ ...filters, page: p })}
        />
      ) : null}
    </div>
  )
}

function PageHeader({ total }: { total: number | undefined }): React.JSX.Element {
  const label = total === undefined ? null : `${String(total)} ${total === 1 ? 'run' : 'runs'}`
  return (
    <div className="flex items-center justify-between">
      <h1 className="text-2xl font-semibold">Run history</h1>
      {label === null ? null : <span className="text-muted-foreground text-sm">{label}</span>}
    </div>
  )
}
